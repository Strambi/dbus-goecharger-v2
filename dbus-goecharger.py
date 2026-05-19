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
import configparser

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusGoeChargerService:
    def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP API v2'):
        config = self._getConfig()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])
        hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
        acPosition = int(config['DEFAULT']['AcPosition'])
        pauseBetweenRequests = int(config['ONPREMISE']['PauseBetweenRequests'])

        if pauseBetweenRequests <= 20:
            raise ValueError("PauseBetweenRequests must be greater than 20")

        self._dbusservice = VeDbusService(
            "{}.http_{:02d}".format(servicename, deviceinstance), register=False)
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        paths_wo_unit = ['/Status', '/Mode']

        data = self._getData('sse,fwv')

        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion',
            'Unknown version, running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', productname)

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

        self._lastUpdate = 0
        self._chargingTime = 0.0

        gobject.timeout_add(pauseBetweenRequests, self._update)
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT'].get('SignOfLifeLog', '0')
        return int(value) if value else 0

    def _getBaseUrl(self):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']
        if accessType == 'OnPremise':
            return "http://%s" % config['ONPREMISE']['Host']
        raise ValueError("AccessType %s is not supported" % accessType)

    def _getData(self, filter_keys):
        """Fetch filtered status data via API v2 GET /api/status?filter=key1,key2,..."""
        url = "%s/api/status?filter=%s" % (self._getBaseUrl(), filter_keys)
        try:
            response = requests.get(url=url, timeout=2)
        except Exception:
            return None

        if not response:
            raise ConnectionError("No response from go-eCharger - %s" % url)

        json_data = response.json()
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        return json_data

    def _setValue(self, key, value):
        """Write a single value via API v2 GET /api/set?key=value"""
        url = "%s/api/set?%s=%s" % (self._getBaseUrl(), key, value)
        try:
            response = requests.get(url=url, timeout=2)
        except Exception as e:
            raise ConnectionError("No response from go-eCharger - %s: %s" % (url, e))

        if not response:
            raise ConnectionError("No response from go-eCharger - %s" % url)

        json_data = response.json()
        if not json_data:
            raise ValueError("Converting response to JSON failed")

        # API v2 returns the updated key-value pair on success
        returned = json_data.get(key)
        expected = str(value)
        if str(returned) == expected:
            return True

        logging.warning("go-eCharger: set %s=%s but got %s" % (key, expected, returned))
        return False

    def _signOfLife(self):
        logging.info("--- sign of life ---")
        logging.info("Last _update(): %s" % self._lastUpdate)
        logging.info("/Ac/Power: %s" % self._dbusservice['/Ac/Power'])
        return True

    def _update(self):
        try:
            # nrg array layout (API v2, same indices as v1 but values can be float):
            # [0] U L1, [1] U L2, [2] U L3, [3] U N
            # [4] I L1, [5] I L2, [6] I L3
            # [7] P L1, [8] P L2, [9] P L3, [10] P N, [11] P Total
            # [12] PF L1, [13] PF L2, [14] PF L3, [15] PF N
            data = self._getData('nrg,wh,eto,alw,amp,ama,car,tma,frc')

            if data is not None:
                nrg = data.get('nrg', [0] * 16)

                self._dbusservice['/Ac/Voltage'] = round(float(nrg[0]), 1)
                self._dbusservice['/Ac/L1/Power'] = round(float(nrg[7]), 1)
                self._dbusservice['/Ac/L2/Power'] = round(float(nrg[8]), 1)
                self._dbusservice['/Ac/L3/Power'] = round(float(nrg[9]), 1)
                self._dbusservice['/Ac/Power'] = round(float(nrg[11]), 1)
                self._dbusservice['/Current'] = round(
                    max(float(nrg[4]), float(nrg[5]), float(nrg[6])), 1)

                # Energy: prefer wh (per session kWh), fall back to eto (total Wh)
                wh = data.get('wh')
                eto = data.get('eto')
                if wh is not None:
                    self._dbusservice['/Ac/Energy/Forward'] = round(float(wh) / 1000.0, 2)
                elif eto is not None:
                    self._dbusservice['/Ac/Energy/Forward'] = round(float(eto) / 1000.0, 2)

                # Charging time
                timeDelta = time.time() - self._lastUpdate
                car = int(data.get('car', 0))
                if car == 2 and self._lastUpdate > 0:
                    self._chargingTime += timeDelta
                elif car == 1:
                    self._chargingTime = 0
                self._dbusservice['/ChargingTime'] = int(self._chargingTime)

                # alw is read-only in API v2 (reflects actual charging permission)
                self._dbusservice['/StartStop'] = 1 if data.get('alw') else 0

                self._dbusservice['/SetCurrent'] = int(data.get('amp', 0))
                self._dbusservice['/MaxCurrent'] = int(data.get('ama', 0))
                self._dbusservice['/Mode'] = 0  # Manual, no auto control

                # Temperature: tma is an array; use first sensor
                tma = data.get('tma')
                if tma and isinstance(tma, list) and len(tma) > 0:
                    self._dbusservice['/MCU/Temperature'] = round(float(tma[0]), 1)
                else:
                    self._dbusservice['/MCU/Temperature'] = 0

                # Map car state to Victron EV charger status
                # car: 0=Unknown/Error, 1=Idle, 2=Charging, 3=WaitCar, 4=Complete, 5=Error
                # Victron: 0=Disconnected, 2=Charging, 3=Charged, 6=WaitingForStart
                status_map = {0: 0, 1: 0, 2: 2, 3: 6, 4: 3, 5: 0}
                self._dbusservice['/Status'] = status_map.get(car, 0)

                logging.debug("/Ac/Power: %s W" % self._dbusservice['/Ac/Power'])
                logging.debug("/Ac/Energy/Forward: %s kWh" % self._dbusservice['/Ac/Energy/Forward'])

                index = self._dbusservice['/UpdateIndex'] + 1
                if index > 255:
                    index = 0
                self._dbusservice['/UpdateIndex'] = index
                self._lastUpdate = time.time()
            else:
                logging.debug("Wallbox not reachable")

        except Exception as e:
            logging.critical('Error at _update', exc_info=e)

        return True

    def _handlechangedvalue(self, path, value):
        logging.info("DBus write: %s = %s" % (path, value))

        if path == '/SetCurrent':
            # amp: desired charging current in A
            return self._setValue('amp', int(value))
        elif path == '/MaxCurrent':
            # ama: absolute maximum current in A
            return self._setValue('ama', int(value))
        elif path == '/StartStop':
            # API v2: use frc (forceState) to control charging
            # frc=0 Neutral, frc=1 Off, frc=2 On
            frc_value = 2 if int(value) == 1 else 1
            return self._setValue('frc', frc_value)
        else:
            logging.info("No mapping for path %s" % path)
            return False


def main():
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % os.path.dirname(os.path.realpath(__file__)))
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

    try:
        logging.info("Start dbus-goecharger API v2")

        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

        _kwh  = lambda p, v: (str(round(v, 2)) + ' kWh')
        _a    = lambda p, v: (str(round(v, 1)) + ' A')
        _w    = lambda p, v: (str(round(v, 1)) + ' W')
        _v    = lambda p, v: (str(round(v, 1)) + ' V')
        _degC = lambda p, v: (str(round(v, 1)) + ' °C')
        _s    = lambda p, v: (str(v) + ' s')

        pvac_output = DbusGoeChargerService(
            servicename='com.victronenergy.evcharger',
            paths={
                '/Ac/Power':          {'initial': 0, 'textformat': _w},
                '/Ac/L1/Power':       {'initial': 0, 'textformat': _w},
                '/Ac/L2/Power':       {'initial': 0, 'textformat': _w},
                '/Ac/L3/Power':       {'initial': 0, 'textformat': _w},
                '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
                '/ChargingTime':      {'initial': 0, 'textformat': _s},
                '/Ac/Voltage':        {'initial': 0, 'textformat': _v},
                '/Current':           {'initial': 0, 'textformat': _a},
                '/SetCurrent':        {'initial': 0, 'textformat': _a},
                '/MaxCurrent':        {'initial': 0, 'textformat': _a},
                '/MCU/Temperature':   {'initial': 0, 'textformat': _degC},
                '/StartStop':         {'initial': 0, 'textformat': lambda p, v: str(v)},
            })

        logging.info('Connected to dbus, starting gobject.MainLoop()')
        mainloop = gobject.MainLoop()
        mainloop.run()

    except Exception as e:
        logging.critical('Error at main', exc_info=e)


if __name__ == "__main__":
    main()
