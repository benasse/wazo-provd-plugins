# -*- coding: utf-8 -*-
# Copyright 2013-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os.path
import re

from provd import tzinform
from provd import synchronize
from provd.devices.config import RawConfigError
from provd.plugins import (
    FetchfwPluginHelper,
    StandardPlugin,
    TemplatePluginHelper
)
from provd.devices.pgasso import (
    BasePgAssociator,
    COMPLETE_SUPPORT,
    IMPROBABLE_SUPPORT,
    UNKNOWN_SUPPORT
)
from provd.servers.http import HTTPNoListingFileService
from provd.util import norm_mac, format_mac
from twisted.internet import defer, threads

logger = logging.getLogger('plugin.wazo-fanvil')


class BaseFanvilHTTPDeviceInfoExtractor(object):
    _PATH_REGEX = re.compile(r'\b(?!0{12})([\da-f]{12})\.cfg$')
    _UA_REGEX = re.compile(r'^Fanvil (?P<model>[X|C][0-9]{1,2}[S|G|V|U|C]?[0-9]?) (?P<version>[0-9.]+) (?P<mac>[\da-f]{12})$')

    def __init__(self, common_files):
        self._COMMON_FILES = common_files

    def extract(self, request, request_type):
        return defer.succeed(self._do_extract(request))

    def _do_extract(self, request):
        dev_info = {}
        dev_info.update(self._extract_from_path(request))
        ua = request.getHeader('User-Agent')
        if ua:
            dev_info.update(self._extract_from_ua(ua))

        return dev_info

    def _extract_from_ua(self, ua):
        # Fanvil X4 2.10.2.6887 0c383e07e16c
        dev_info = {}
        m = self._UA_REGEX.search(ua)
        if m:
            dev_info['vendor'] = 'Fanvil'
            dev_info['model'] = m.group('model').decode('ascii')
            dev_info['version'] = m.group('version').decode('ascii')
            dev_info['mac'] = norm_mac(m.group('mac').decode('ascii'))
        return dev_info

    def _extract_from_path(self, request):
        filename = os.path.basename(request.path)
        device_info = self._COMMON_FILES.get(filename)
        if device_info:
            return {u'vendor': u'Fanvil',
                    u'model': device_info[0]}

        m = self._PATH_REGEX.search(request.path)
        if m:
            raw_mac = m.group(1)
            mac = norm_mac(raw_mac.decode('ascii'))
            return {u'mac': mac}
        return {}


class BaseFanvilPgAssociator(BasePgAssociator):

    def __init__(self, models):
        BasePgAssociator.__init__(self)
        self._models = models

    def _do_associate(self, vendor, model, version):
        if vendor == u'Fanvil':
            if model in self._models:
                return COMPLETE_SUPPORT
            return UNKNOWN_SUPPORT
        return IMPROBABLE_SUPPORT


class BaseFanvilPlugin(StandardPlugin):
    _ENCODING = 'UTF-8'
    _LOCALE = {}
    _TZ_INFO = {}
    _SIP_DTMF_MODE = {
        u'RTP-in-band': u'0',
        u'RTP-out-of-band': u'1',
        u'SIP-INFO': u'2',
    }
    _SIP_TRANSPORT = {
        u'udp': u'0',
        u'tcp': u'1',
        u'tls': u'3',
    }

    def __init__(self, app, plugin_dir, gen_cfg, spec_cfg):
        StandardPlugin.__init__(self, app, plugin_dir, gen_cfg, spec_cfg)
        # update to use the non-standard tftpboot directory
        self._base_tftpboot_dir = self._tftpboot_dir
        self._tftpboot_dir = os.path.join(self._tftpboot_dir, 'Fanvil')

        self._tpl_helper = TemplatePluginHelper(plugin_dir)

        downloaders = FetchfwPluginHelper.new_downloaders(gen_cfg.get('proxies'))
        fetchfw_helper = FetchfwPluginHelper(plugin_dir, downloaders)
        # update to use the non-standard tftpboot directory
        fetchfw_helper.root_dir = self._tftpboot_dir

        self.services = fetchfw_helper.services()
        self.http_service = HTTPNoListingFileService(self._base_tftpboot_dir)

    def _dev_specific_filename(self, device):
        # Return the device specific filename (not pathname) of device
        fmted_mac = format_mac(device[u'mac'], separator='', uppercase=False)
        return fmted_mac + '.cfg'

    def _check_config(self, raw_config):
        if u'http_port' not in raw_config:
            raise RawConfigError('only support configuration via HTTP')

    def _check_device(self, device):
        if u'mac' not in device:
            raise Exception('MAC address needed for device configuration')

    def configure(self, device, raw_config):
        self._check_config(raw_config)
        self._check_device(device)
        self._check_lines_password(raw_config)
        self._add_timezone(device, raw_config)
        self._add_locale(device, raw_config)
        self._add_sip_transport(raw_config)
        self._update_lines(raw_config)
        self._add_fkeys(raw_config)
        filename = self._dev_specific_filename(device)
        tpl = self._tpl_helper.get_dev_template(filename, device)

        path = os.path.join(self._tftpboot_dir, filename)
        self._tpl_helper.dump(tpl, raw_config, path, self._ENCODING)

    def deconfigure(self, device):
        self._remove_configuration_file(device)

    def configure_common(self, raw_config):
        for filename, (_, fw_filename, tpl_filename) in self._COMMON_FILES.iteritems():
            tpl = self._tpl_helper.get_template('common/%s' % tpl_filename)
            dst = os.path.join(self._tftpboot_dir, filename)
            raw_config[u'XX_fw_filename'] = fw_filename
            self._tpl_helper.dump(tpl, raw_config, dst, self._ENCODING)

    def _remove_configuration_file(self, device):
        path = os.path.join(self._tftpboot_dir, self._dev_specific_filename(device))
        try:
            os.remove(path)
        except OSError as e:
            logger.info('error while removing configuration file: %s', e)

    if hasattr(synchronize, 'standard_sip_synchronize'):
        def synchronize(self, device, raw_config):
            return synchronize.standard_sip_synchronize(device)

    else:
        # backward compatibility with older wazo-provd server
        def synchronize(self, device, raw_config):
            try:
                ip = device[u'ip'].encode('ascii')
            except KeyError:
                return defer.fail(Exception('IP address needed for device synchronization'))
            else:
                sync_service = synchronize.get_sync_service()
                if sync_service is None or sync_service.TYPE != 'AsteriskAMI':
                    return defer.fail(Exception('Incompatible sync service: %s' % sync_service))
                else:
                    return threads.deferToThread(sync_service.sip_notify, ip, 'check-sync')

    def get_remote_state_trigger_filename(self, device):
        if u'mac' not in device:
            return None

        return self._dev_specific_filename(device)

    def _check_lines_password(self, raw_config):
        for line in raw_config[u'sip_lines'].itervalues():
            if line[u'password'] == u'autoprov':
                line[u'password'] = u''

    def _format_dst_change(self, suffix, dst_change):
        lines = []
        lines.append(u'<DST_%s_Mon>%d</DST_%s_Mon>' % (suffix, dst_change['month'], suffix))
        lines.append(u'<DST_%s_Hour>%d</DST_%s_Hour>' % (suffix, min(dst_change['time'].as_hours, 23), suffix))
        if dst_change['day'].startswith('D'):
            lines.append(u'<DST_%s_Wday>%s</DST_%s_Wday>' % (suffix, dst_change['day'][1:], suffix))
        else:
            week, weekday = dst_change['day'][1:].split('.')
            if week == '5':
                lines.append(u'<DST_%s_Week>-1</DST_%s_Week>' % (suffix, suffix))
            else:
                lines.append(u'<DST_%s_Week>%s</DST_%s_Week>' % (suffix, week, suffix))
            lines.append(u'<DST_%s_Wday>%s</DST_%s_Wday>' % (suffix, weekday, suffix))
        lines.append(u'<DST_%s_Min>0</DST_%s_Min>' % (suffix, suffix))
        return lines

    def _format_tzinfo(self, device, tzinfo):
        lines = []
        utc = tzinfo['utcoffset'].as_hours
        utc_list = self._TZ_INFO[utc]
        for time_zone_name, time_zone in utc_list:
            lines.append(u'<Time_Zone>%s</Time_Zone>' % (time_zone))
            lines.append(u'<Time_Zone_Name>%s</Time_Zone_Name>' % (time_zone_name))
        if tzinfo['dst'] is None:
            lines.append(u'<Enable_DST>0</Enable_DST>')
        else:
            lines.append(u'<Enable_DST>2</Enable_DST>')
            lines.append(u'<DST_Min_Offset>%d</DST_Min_Offset>' % (min(tzinfo['dst']['save'].as_minutes, 60)))
            lines.extend(self._format_dst_change('Start', tzinfo['dst']['start']))
            lines.extend(self._format_dst_change('End', tzinfo['dst']['end']))
        return u'\n'.join(lines)

    def _add_timezone(self, device, raw_config):
        if u'timezone' in raw_config:
            try:
                tzinfo = tzinform.get_timezone_info(raw_config[u'timezone'])
            except tzinform.TimezoneNotFoundError, e:
                logger.info('Unknown timezone: %s', e)
            else:
                raw_config[u'XX_timezone'] = self._format_tzinfo(device, tzinfo)

    def _add_locale(self, device, raw_config):
        locale = raw_config.get(u'locale')
        model_locales = self._LOCALE
        if locale in model_locales:
            raw_config[u'XX_locale'] = model_locales[locale]

    def _update_lines(self, raw_config):
        default_dtmf_mode = raw_config.get(u'sip_dtmf_mode', 'SIP-INFO')
        for line in raw_config[u'sip_lines'].itervalues():
            line['XX_dtmf_mode'] = self._SIP_DTMF_MODE[line.get(u'dtmf_mode', default_dtmf_mode)]

    def _add_sip_transport(self, raw_config):
        raw_config['X_sip_transport_protocol'] = self._SIP_TRANSPORT[raw_config.get(u'sip_transport', u'udp')]

    def _format_funckey_speeddial(self, funckey_no, funckey_dict):
        lines = []
        lines.append(u'<Function_Key_Entry>')
        lines.append(u'<ID>Fkey%d</ID>' % (int(funckey_no) + 1))
        lines.append(u'<Type>1</Type>')
        lines.append(u'<Value>%s@%s/f</Value>' % (funckey_dict[u'value'], funckey_dict[u'line']))
        lines.append(u'<Title>%s</Title>' % (funckey_dict[u'label']))
        lines.append(u'</Function_Key_Entry>')
        return lines

    def _format_funckey_blf(self, funckey_no, funckey_dict, exten_pickup_call=None):
        # Be warned that blf works only for DSS keys.
        lines = []
        lines.append(u'<Function_Key_Entry>')
        lines.append(u'<ID>Fkey%d</ID>' % (int(funckey_no) + 1))
        lines.append(u'<Type>1</Type>')
        if exten_pickup_call:
            lines.append(u'<Value>%s@%s/ba%s%s</Value>' % (funckey_dict[u'value'], funckey_dict[u'line'],
                                                           exten_pickup_call, funckey_dict[u'value']))
        else:
            lines.append(u'<Value>%s@%s/ba</Value>' % (funckey_dict[u'value'], funckey_dict[u'line']))
        lines.append(u'<Title>%s</Title>' % (funckey_dict[u'label']))
        lines.append(u'</Function_Key_Entry>')
        return lines

    def _format_funckey_call_park(self, funckey_no, funckey_dict):
        lines = []
        lines.append(u'<Function_Key_Entry>')
        lines.append(u'<ID>Fkey%d</ID>' % (int(funckey_no) + 1))
        lines.append(u'<Type>1</Type>')
        lines.append(u'<Value>%s@%s/c</Value>' % (funckey_dict[u'value'], funckey_dict[u'line']))
        lines.append(u'<Title>%s</Title>' % (funckey_dict[u'label']))
        lines.append(u'</Function_Key_Entry>')
        return lines

    def _add_fkeys(self, raw_config):
        lines = []
        exten_pickup_call = raw_config.get('exten_pickup_call')
        for funckey_no, funckey_dict in raw_config[u'funckeys'].iteritems():
            keynum = int(funckey_no)
            funckey_type = funckey_dict[u'type']
            if funckey_type == u'speeddial':
                lines.extend(self._format_funckey_speeddial(funckey_no, funckey_dict))
            elif funckey_type == u'blf':
                if keynum <= 12:
                    lines.extend(self._format_funckey_blf(funckey_no, funckey_dict,
                                                          exten_pickup_call))
                else:
                    logger.info('For Fanvil, blf is only available on DSS keys')
                    lines.extend(self._format_funckey_speeddial(funckey_no, funckey_dict))
            elif funckey_type == u'park':
                lines.extend(self._format_funckey_call_park(funckey_no, funckey_dict))
            else:
                logger.info('Unsupported funckey type: %s', funckey_type)
                continue
        raw_config[u'XX_fkeys'] = u'\n'.join(lines)
