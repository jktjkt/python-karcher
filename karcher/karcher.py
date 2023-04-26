# -----------------------------------------------------------
# Copyright (c) 2023 Lauris BH
# SPDX-License-Identifier: MIT
# -----------------------------------------------------------

import collections
import json
import requests
import urllib.parse

from .auth import Domains, Session
from .consts import APP_VERSION_CODE, APP_VERSION_NAME, PROJECT_TYPE, PROTOCOL_VERSION, REGION_URLS, TENANT_ID, Region, Language
from .device import Device
from .exception import KarcherHomeAccessDenied, KarcherHomeException, handle_error_code
from .map import Map
from .utils import decrypt, decrypt_map, encrypt, get_nonce, get_random_string, get_timestamp, is_email, md5

class KarcherHome:
    """Main class to access Karcher Home Robots API"""

    def __init__(self, region: Region = Region.EU):
        """Initialize Karcher Home Robots API"""

        super().__init__()
        self._base_url = REGION_URLS[region]
        self._mqtt_url = None
        self._language = Language.EN

        d = self.get_urls()
        # Update base URLs
        if d.app_api != '':
            self._base_url = d.app_api
        if d.mqtt != '':
            self._mqtt_url = d.mqtt

    def _request(self, sess: Session, method: str, url: str, **kwargs):
        session = requests.Session()
        # TODO: Fix SSL
        requests.packages.urllib3.disable_warnings()
        session.verify = False

        headers = {}
        if kwargs.get('headers') is not None:
            headers = kwargs['headers']

        headers['User-Agent'] = 'Android_' + TENANT_ID
        auth = ''
        if sess != None and sess.auth_token != '':
            auth = sess.auth_token
            headers['authorization'] = auth
        if sess != None and sess.user_id != '':
            headers['id'] = sess.user_id
        headers['tenantId'] = TENANT_ID

        # Sign request
        nonce = get_nonce()
        ts = str(get_timestamp())
        data = ''
        if method == 'GET':
            params = kwargs.get('params') or {}
            if type(params) == str:
                params = urllib.parse.parse_qs(params)
            buf = urllib.parse.urlencode(params)
            data = buf
            kwargs['params'] = buf
        elif method == 'POST' or method == 'PUT':
            v = params = kwargs.get('json') or {}
            if type(v) == dict:
                v = collections.OrderedDict(v.items())
                for key, val in v.items():
                    data += key
                    if val == None:
                        data += 'null'
                    elif type(val) == str:
                        data += val
                    elif type(val) == dict:
                        data += json.dumps(val, separators=(',', ':'))
                    else:
                        data += str(val)
                kwargs['json'] = v

        headers['sign'] = md5(auth + ts + nonce + data)
        headers['ts'] = ts
        headers['nonce'] = nonce

        kwargs['headers'] = headers
        return session.request(method, self._base_url + url, **kwargs)

    def _download(self, url):
        session = requests.Session()
        headers = {
            'User-Agent': 'Android_' + TENANT_ID,
        }

        resp = session.get(url, headers=headers)
        if resp.status_code != 200:
            raise KarcherHomeException(-1, 'HTTP error: ' + str(resp.status_code))

        return resp.content

    def _process_response(self, resp, prop = None):
        if resp.status_code != 200:
            raise KarcherHomeException(-1, 'HTTP error: ' + str(resp.status_code))
        data = resp.json()
        # Check for error response
        if data['code'] != 0:
            handle_error_code(data['code'], data['msg'])
        # Check for empty response
        if 'result' not in data:
            return None
        # Handle special response types
        result = data['result']
        if type(result) == str:
            raise KarcherHomeException(-2, 'Invalid response: ' + result)
        if prop != None:
            return json.loads(decrypt(result[prop]))
        return result

    def get_urls(self):
        """Get URLs for API and MQTT."""

        resp = self._request(None, 'GET', '/network-service/domains/list', params={
            'tenantId': TENANT_ID,
            'productModeCode': PROJECT_TYPE,
            'version': PROTOCOL_VERSION,
        })

        d = self._process_response(resp, 'domain')
        return Domains(**d)

    def login(self, username, password, register_id = None):
        """Login using provided credentials."""

        if register_id == None or register_id == '':
            register_id = get_random_string(19)

        if not is_email(username):
            username = '86-' + username

        resp = self._request(None, 'POST', '/user-center/auth/login', json={
            'tenantId': TENANT_ID,
            'lang': str(self._language),
            'token': None,
            'userId': None,
            'password': encrypt(password),
            'username': encrypt(username),
            'authcode': None,
            'projectType': PROJECT_TYPE,
            'versionCode': APP_VERSION_CODE,
            'versionName': APP_VERSION_NAME,
            'phoneBrand': encrypt('xiaomi_mi 9'),
            'phoneSys': 1,
            'noticeSetting': {
                'andIpad': register_id,
                'android': register_id,
            },
        })

        d = self._process_response(resp)
        sess = Session(**d)
        sess.register_id = register_id

        return sess

    def logout(self, sess: Session):
        """End current session.
        
        This will also reset the session object.
        """
        if sess.auth_token == '' or sess.user_id == '':
            sess.reset()
            return
        
        self._process_response(self._request(sess, 'POST', '/user-center/auth/logout'))
        sess.reset()

    def get_devices(self, sess: Session):
        """Get all user devices."""

        if sess == None or sess.auth_token == '' or sess.user_id == '':
            raise KarcherHomeAccessDenied('Not authorized')

        resp = self._request(sess, 'GET', '/smart-home-service/smartHome/user/getDeviceInfoByUserId/' + sess.user_id)

        return [Device(**d) for d in self._process_response(resp)]

    def get_map_data(self, sess: Session, dev: Device, map: int = 1):
        # <tenantId>/<modeType>/<deviceSn>/01-01-2022/map/temp/0046690461_<deviceSn>_1
        mapDir = TENANT_ID + '/' + dev.product_mode_code + '/' +\
            dev.sn + '/01-01-2022/map/temp/0046690461_' + dev.sn + '_' + str(map)

        resp = self._request(sess, 'POST', '/storage-management/storage/aws/getAccessUrl', json={
            'dir': mapDir,
            'countryCode': sess.get_country_code(),
            'serviceType': 2,
            'tenantId': TENANT_ID,
        })

        d = self._process_response(resp)
        downloadUrl = d['url']
        if 'cdnDomain' in d and d['cdnDomain'] != '':
            downloadUrl = 'https://' + d['cdnDomain'] + '/' + d['dir']

        d = self._download(downloadUrl)
        data = decrypt_map(dev.sn, dev.mac, dev.product_id, d)
        if map == 1 or map == 2:
            return Map.parse(data)
        else:
            return json.loads(data)

    def get_families(self, sess: Session):

        if sess == None or sess.auth_token == '' or sess.user_id == '':
            raise KarcherHomeAccessDenied('Not authorized')
        
        resp = self._request(sess, 'GET', '/smart-home-service/smartHome/familyInfo/list/' + sess.user_id)

        return self._process_response(resp)

    def get_consumables(self, sess: Session, familyID: str):

        if sess == None or sess.auth_token == '' or sess.user_id == '':
            raise KarcherHomeAccessDenied('Not authorized')
        
        resp = self._request(sess, 'GET', '/smart-home-service/smartHome/consumablesInfo/getConsumablesInfoByFamilyId/' + familyID)

        return self._process_response(resp)
