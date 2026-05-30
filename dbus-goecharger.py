#!/usr/bin/env python

import platform
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import time
import requests
import json
import configparser

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService
import dbus

CONFIG_FILE = "%s/config.ini" % os.path.dirname(os.path.realpath(__file__))


def _getConfig():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    return config


def _getChargerSections(config):
    """Return all sections whose name starts with 'CHARGER'."""
    return [s for s in config.sections() if s.upper().startswith('CHARGER')]


class DbusGoeChargerService:
    def __init__(self, servicename, charger_section, paths, productname='go-eCharger'):
        """
        charger_section: name of the config.ini section for this charger,
                         e.g. 'CHARGER1'. Per-charger values are read from
                         this section; shared defaults fall back to [DEFAULT].
        """
        config = _getConfig()
        section = config[charger_section]

        self._charger_section = charger_section
        self._host = section['Host']
        self._grid_service = section.get('GridService', '').strip()
        self._grid_path = section.get('GridPath', '/Ac/Power').strip()

        deviceinstance      = int(section.get('Deviceinstance', section.get('Deviceinstance', '43')))
        hardwareVersion     = int(section.get('HardwareVersion', '4'))
        acPosition          = int(section.get('AcPosition', '0'))
        pauseBetweenRequests = int(section.get('PauseBetweenRequests', '5000'))
        customName          = section.get('CustomName', productname)
        signOfLifeInterval  = int(section.get('SignOfLifeLog', '0'))
        connection          = 'go-eCharger HTTP API v2 (%s)' % self._host

        if pauseBetweenRequests <= 20:
            raise ValueError("[%s] PauseBetweenRequests must be greater than 20" % charger_section)

        self._pauseBetweenRequests = pauseBetweenRequests
        self._signOfLifeInterval   = signOfLifeInterval
        self._paths                = paths
        self._lastUpdate           = 0
        self._chargingTime         = 0.0
        self._sessionStartEto      = None   # eto baseline for session energy fallback
        self._sessionEnergy        = 0.0    # self-integrated session energy in Wh
        self._lmo                  = 1      # last known go-eCharger logic mode (default: Basic)

        self._system_bus = dbus.SystemBus()

        self._dbusservice = VeDbusService(
            "{}.http_{:02d}".format(servicename, deviceinstance),
            bus=dbus.SystemBus(private=True),
            register=False)

        logging.info("[%s] Initialising – host=%s deviceinstance=%d" % (
            charger_section, self._host, deviceinstance))

        paths_wo_unit = ['/Status']

        data = self._getData('sse,fwv')

        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion',
            'Unknown version, running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', customName)

        if data:
            fwv = data.get('fwv', 'unknown')
            try:
                fwv = int(str(fwv).replace('.', ''))
            except Exception:
                pass
            self._dbusservice.add_path('/FirmwareVersion', fwv)
            self._dbusservice.add_path('/Serial', data.get('sse', 'unknown'))

        self._dbusservice.add_path('/HardwareVersion', hardwareVersion)
        self._dbusservice.add_path('/Connected', 1)
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/Position', acPosition)

        for path in paths_wo_unit:
            self._dbusservice.add_path(path, None)

        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path, settings['initial'],
                gettextcallback=settings['textformat'],
                writeable=True,
                onchangecallback=self._handlechangedvalue)

        self._dbusservice.register()

        gobject.timeout_add(pauseBetweenRequests, self._update)
        if signOfLifeInterval > 0:
            gobject.timeout_add(signOfLifeInterval * 60 * 1000, self._signOfLife)

    # -------------------------------------------------------------------------
    # HTTP helpers
    # -------------------------------------------------------------------------

    def _baseUrl(self):
        return "http://%s" % self._host

    def _getData(self, filter_keys):
        """GET /api/status?filter=key1,key2,..."""
        url = "%s/api/status?filter=%s" % (self._baseUrl(), filter_keys)
        try:
            response = requests.get(url=url, timeout=2)
        except Exception:
            return None

        if not response:
            raise ConnectionError("No response from go-eCharger – %s" % url)

        json_data = response.json()
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        return json_data

    def _setValue(self, key, value):
        """GET /api/set?key=value"""
        url = "%s/api/set?%s=%s" % (self._baseUrl(), key, value)
        try:
            response = requests.get(url=url, timeout=2)
        except Exception as e:
            raise ConnectionError("No response from go-eCharger – %s: %s" % (url, e))

        if not response:
            raise ConnectionError("No response from go-eCharger – %s" % url)

        json_data = response.json()
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        returned = json_data.get(key)
        if str(returned) == str(value):
            return True

        logging.warning("[%s] set %s=%s but got %s" % (
            self._charger_section, key, value, returned))
        return False

    # -------------------------------------------------------------------------
    # Timer callbacks
    # -------------------------------------------------------------------------

    def _signOfLife(self):
        logging.info("[%s] sign of life – last update: %s  /Ac/Power: %s" % (
            self._charger_section, self._lastUpdate, self._dbusservice['/Ac/Power']))
        return True

    def _update(self):
        try:
            # nrg array layout (values can be float since firmware 053.3):
            # [0] U L1  [1] U L2  [2] U L3  [3] U N
            # [4] I L1  [5] I L2  [6] I L3
            # [7] P L1  [8] P L2  [9] P L3  [10] P N  [11] P Total
            # [12] PF L1  [13] PF L2  [14] PF L3  [15] PF N
            data = self._getData('nrg,wh,eto,alw,amp,ama,car,tma,frc,cdi,lmo')

            if data is not None:
                nrg = data.get('nrg', [0] * 16)

                self._dbusservice['/Ac/Voltage']   = round(float(nrg[0]), 1)
                self._dbusservice['/Ac/L1/Power']  = round(float(nrg[7]), 1)
                self._dbusservice['/Ac/L2/Power']  = round(float(nrg[8]), 1)
                self._dbusservice['/Ac/L3/Power']  = round(float(nrg[9]), 1)
                self._dbusservice['/Ac/Power']     = round(float(nrg[11]), 1)
                self._dbusservice['/Current']      = round(
                    max(float(nrg[4]), float(nrg[5]), float(nrg[6])), 1)

                # Energy: wh = session energy (Wh), eto = total odometer (Wh)
                wh  = data.get('wh')
                eto = data.get('eto')
                car = int(data.get('car', 0))
                timeDelta = time.time() - self._lastUpdate if self._lastUpdate > 0 else 0
                logging.debug("[%s] raw wh=%s eto=%s car=%s timeDelta=%.1fs" % (
                    self._charger_section, wh, eto, car, timeDelta))

                # Reset session state on disconnect (car==1) regardless of energy source
                if car == 1:
                    self._sessionEnergy = 0.0
                    self._sessionStartEto = None

                if wh is not None:
                    # charger reports session energy directly (0 = session just started)
                    self._sessionEnergy = float(wh)
                elif eto is not None:
                    # fallback 1: track session energy via total odometer delta
                    eto_wh = float(eto)
                    if self._sessionStartEto is None:
                        self._sessionStartEto = eto_wh
                    self._sessionEnergy = max(0.0, eto_wh - self._sessionStartEto)
                elif car == 2 and timeDelta > 0:
                    # fallback 2: integrate power over time ourselves
                    self._sessionEnergy += float(nrg[11]) * timeDelta / 3600.0

                energy_kwh = round(self._sessionEnergy / 1000.0, 2)
                self._dbusservice['/Ac/Energy/Forward'] = energy_kwh
                self._dbusservice['/Session/Energy']    = energy_kwh

                # Charging time: prefer hardware value (cdi), fallback to self-tracking
                cdi = data.get('cdi')
                if cdi and isinstance(cdi, dict) and cdi.get('type') == 2 and cdi.get('value') is not None:
                    # cdi.value = milliseconds since session start
                    self._chargingTime = float(cdi['value']) / 1000.0
                elif car == 2 and timeDelta > 0:
                    self._chargingTime += timeDelta
                elif car == 1:
                    self._chargingTime = 0
                charging_time = int(self._chargingTime)
                self._dbusservice['/ChargingTime']  = charging_time
                self._dbusservice['/Session/Time']  = charging_time

                # alw is read-only in API v2
                self._dbusservice['/StartStop'] = 1 if data.get('alw') else 0
                self._dbusservice['/SetCurrent'] = int(data.get('amp', 0))
                self._dbusservice['/MaxCurrent'] = int(data.get('ama', 0))
                # go-eCharger lmo: 1=Default  3=Eco  4=Next Trip
                # lmo=3 or 4 → force Venus OS Manual (0), block mode changes
                # lmo=1      → Venus OS mode 0/1/2 freely selectable
                lmo = data.get('lmo')
                if lmo is not None:
                    self._lmo = int(lmo)
                current_mode = self._dbusservice['/Mode']
                logging.debug("[%s] lmo=%s (type=%s) current_mode=%s" % (
                    self._charger_section, lmo, type(lmo).__name__, current_mode))
                if self._lmo in (4, 5):  # 4=Eco, 5=Daily trip → force Manual
                    if current_mode != 0:
                        logging.info("[%s] go-eCharger lmo=%s → forcing Venus OS Mode=0 (Manual)" % (
                            self._charger_section, self._lmo))
                        self._dbusservice['/Mode'] = 0

                # Temperature: tma is an array of sensors
                tma = data.get('tma')
                if tma and isinstance(tma, list) and len(tma) > 0 and tma[0] is not None:
                    self._dbusservice['/MCU/Temperature'] = round(float(tma[0]), 1)
                else:
                    self._dbusservice['/MCU/Temperature'] = 0

                # Map car state → Victron EV charger status
                # car:     0=Unknown/Error  1=Idle  2=Charging  3=WaitCar  4=Complete  5=Error
                # Victron: 0=Disconnected   2=Charging          6=WaitingForStart  3=Charged
                status_map = {0: 0, 1: 0, 2: 2, 3: 6, 4: 3, 5: 0}
                self._dbusservice['/Status'] = status_map.get(car, 0)

                if self._grid_service:
                    try:
                        obj = self._system_bus.get_object(self._grid_service, self._grid_path)
                        grid_power = int(round(obj.GetValue()))
                        # pGrid must be pushed via JSON ids format: /api/set?ids={"pGrid":X,"pPv":0,"pAkku":0}
                        # Must be updated within 10 s or the charger disables PV surplus mode
                        payload = json.dumps({"pGrid": grid_power, "pPv": 0, "pAkku": 0})
                        url = "%s/api/set" % self._baseUrl()
                        response = requests.get(url, params={'ids': payload}, timeout=5)
                        logging.debug("[%s] Pushed pGrid=%d W to charger" % (
                            self._charger_section, grid_power))
                    except Exception as e:
                        logging.warning("[%s] Could not push pGrid from D-Bus (%s%s): %s" % (
                            self._charger_section, self._grid_service, self._grid_path, e))

                logging.debug("[%s] /Ac/Power=%s W  /Ac/Energy/Forward=%s kWh" % (
                    self._charger_section,
                    self._dbusservice['/Ac/Power'],
                    self._dbusservice['/Ac/Energy/Forward']))

                index = self._dbusservice['/UpdateIndex'] + 1
                if index > 255:
                    index = 0
                self._dbusservice['/UpdateIndex'] = index
                self._lastUpdate = time.time()

            else:
                logging.debug("[%s] Wallbox not reachable" % self._charger_section)

        except Exception as e:
            logging.critical('[%s] Error at _update' % self._charger_section, exc_info=e)

        return True

    def _handlechangedvalue(self, path, value):
        logging.info("[%s] DBus write: %s = %s" % (self._charger_section, path, value))

        if path == '/SetCurrent':
            return self._setValue('amp', int(value))
        elif path == '/MaxCurrent':
            return self._setValue('ama', int(value))
        elif path == '/Mode':
            # Only allow mode changes when go-eCharger is in Default mode (lmo=1)
            if self._lmo in (4, 5) and int(value) != 0:  # block Auto/Scheduled in Eco/Daily trip
                logging.info("[%s] Mode change to %s blocked: go-eCharger lmo=%s (not Default)" % (
                    self._charger_section, value, self._lmo))
                return False
            return True  # accept locally, Venus OS handles auto/scheduled control
        elif path == '/StartStop':
            # frc: 0=Neutral  1=Force Off  2=Force On
            frc_value = 2 if int(value) == 1 else 1
            return self._setValue('frc', frc_value)
        else:
            logging.info("[%s] No mapping for path %s" % (self._charger_section, path))
            return False


def main():
    config = _getConfig()
    logging_level = config['DEFAULT'].get('Logging', 'ERROR').upper()

    logging.basicConfig(
        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        level=logging_level,
        handlers=[
            RotatingFileHandler(
                "%s/current.log" % os.path.dirname(os.path.realpath(__file__)),
                maxBytes=100000, backupCount=3),
            logging.StreamHandler()
        ])

    charger_sections = _getChargerSections(config)
    if not charger_sections:
        logging.critical("No [CHARGERx] sections found in config.ini – nothing to do.")
        sys.exit(1)

    logging.info("Starting dbus-goecharger API v2 – %d charger(s) configured: %s" % (
        len(charger_sections), ', '.join(charger_sections)))

    try:
        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

        _kwh  = lambda p, v: (str(round(v, 2)) + ' kWh')
        _a    = lambda p, v: (str(round(v, 1)) + ' A')
        _w    = lambda p, v: (str(round(v, 1)) + ' W')
        _v    = lambda p, v: (str(round(v, 1)) + ' V')
        _degC = lambda p, v: (str(round(v, 1)) + ' °C')
        _s    = lambda p, v: (str(v) + ' s')

        paths = {
            '/Ac/Power':          {'initial': 0,   'textformat': _w},
            '/Ac/L1/Power':       {'initial': 0,   'textformat': _w},
            '/Ac/L2/Power':       {'initial': 0,   'textformat': _w},
            '/Ac/L3/Power':       {'initial': 0,   'textformat': _w},
            '/Ac/Energy/Forward': {'initial': 0.0, 'textformat': _kwh},
            '/Session/Energy':    {'initial': 0.0, 'textformat': _kwh},  # Venus OS gui-v2
            '/ChargingTime':      {'initial': 0,   'textformat': _s},
            '/Session/Time':      {'initial': 0,   'textformat': _s},    # Venus OS gui-v2
            '/Ac/Voltage':        {'initial': 0,   'textformat': _v},
            '/Current':           {'initial': 0,   'textformat': _a},
            '/SetCurrent':        {'initial': 0,   'textformat': _a},
            '/MaxCurrent':        {'initial': 0,   'textformat': _a},
            '/MCU/Temperature':   {'initial': 0,   'textformat': _degC},
            '/StartStop':         {'initial': 0,   'textformat': lambda p, v: str(v)},
            '/Mode':              {'initial': 0,   'textformat': lambda p, v: str(v)},
        }

        services = []
        for section in charger_sections:
            svc = DbusGoeChargerService(
                servicename='com.victronenergy.evcharger',
                charger_section=section,
                paths=paths)
            services.append(svc)
            logging.info("Charger [%s] registered on D-Bus." % section)

        logging.info('All chargers connected – starting gobject.MainLoop()')
        mainloop = gobject.MainLoop()
        mainloop.run()

    except Exception as e:
        logging.critical('Error at main', exc_info=e)


if __name__ == "__main__":
    main()
