#      Copyright 2020. ThingsBoard
#
#      Licensed under the Apache License, Version 2.0 (the "License");
#      you may not use this file except in compliance with the License.
#      You may obtain a copy of the License at
#
#          http://www.apache.org/licenses/LICENSE-2.0
#
#      Unless required by applicable law or agreed to in writing, software
#      distributed under the License is distributed on an "AS IS" BASIS,
#      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#      See the License for the specific language governing permissions and
#      limitations under the License.

from threading import Thread
from queue import Queue
from random import choice
from string import ascii_lowercase
from time import sleep, time
from thingsboard_gateway.tb_utility.tb_utility import TBUtility
from thingsboard_gateway.connectors.connector import Connector, log
from thingsboard_gateway.connectors.request.json_request_uplink_converter import JsonRequestUplinkConverter

import requests
from requests import Timeout
from requests.exceptions import RequestException
requests.packages.urllib3.util.ssl_.DEFAULT_CIPHERS += ':ADH-AES128-SHA256'


class RequestConnector(Connector, Thread):
    def __init__(self, gateway, config, connector_type):
        super().__init__()
        self.__config = config
        self.__connector_type = connector_type
        self.__gateway = gateway
        self.__security = self.__config["security"]
        self.__host = None
        self.__service_headers = {}
        if "http://" in self.__config["host"].lower() or "https://" in self.__config["host"].lower():
            self.__host = self.__config["host"]
        else:
            self.__host = "http://" + self.__config["host"]
        self.__ssl_verify = self.__config.get("SSLVerify", False)
        self.setName(self.__config.get("name", "".join(choice(ascii_lowercase) for _ in range(5))))
        self.daemon = True
        self.__connected = False
        self.__stopped = False
        self.__requests_in_progress = []
        self.__convert_queue = Queue(1000000)
        self.__fill_requests()
        log.debug(connector_type)

    def run(self):
        while not self.__stopped:
            if self.__requests_in_progress:
                for request in self.__requests_in_progress:
                    if time() >= request["next_time"]:
                        thread = Thread(target=self.__send_request, args=(request, self.__convert_queue, log), daemon=True, name="Request to endpoint \'%s\' Thread" % (request["config"].get("url")))
                        thread.start()
            else:
                sleep(.1)
            self.__process_data()

    def on_attributes_update(self, content):
        pass

    def server_side_rpc_handler(self, content):
        pass

    def __fill_requests(self):
        log.debug(self.__config["mapping"])
        for endpoint in self.__config["mapping"]:
            try:
                log.debug(endpoint)
                converter = None
                if endpoint["converter"]["type"] == "custom":
                    module = TBUtility.check_and_import(self.__connector_type, endpoint["converter"]["extension"])
                    if module is not None:
                        log.debug('Custom converter for url %s - found!', endpoint["url"])
                        converter = module(endpoint)
                    else:
                        log.error("\n\nCannot find extension module for %s url.\n\Please check your configuration.\n", endpoint["url"])
                else:
                    converter = JsonRequestUplinkConverter(endpoint)
                self.__requests_in_progress.append({"config": endpoint,
                                                    "converter": converter,
                                                    "next_time": time(),
                                                    "request": requests.request})
            except Exception as e:
                log.exception(e)

    def __send_request(self, request, converter_queue, log):
        url = ""
        try:
            request["next_time"] = time() + request["config"].get("scanPeriod", 10)
            request_url_from_config = request["config"]["url"]
            request_url_from_config = str('/' + request_url_from_config) if request_url_from_config[0] != '/' else request_url_from_config
            log.debug(request_url_from_config)
            url = self.__host + request_url_from_config
            log.debug(url)
            request_timeout = request["config"].get("timeout", 1)
            params = {
                "method": request["config"].get("httpMethod", "GET"),
                "url": url,
                "timeout": request_timeout,
                "allow_redirects": request.get("allowRedirects", False),
                "verify": self.__ssl_verify,
            }
            log.debug(url)
            if request["config"].get("httpHeaders") is not None:
                params["headers"] = request["config"]["httpHeaders"]
            log.debug("Request to %s will be sent", url)
            response = request["request"](**params)
            if response and response.ok:
                if not converter_queue.full():
                    data_to_storage = [url, request["converter"]]
                    try:
                        data_to_storage.append(response.json())
                    except UnicodeDecodeError:
                        data_to_storage.append(response.content())
                    if len(data_to_storage) == 3:
                        converter_queue.put(data_to_storage)
            else:
                log.error("Request to URL: %s finished with code: %i", url, response.status_code)
        except Timeout:
            log.error("Timeout error on request %s.", url)
        except RequestException as e:
            log.error("Cannot connect to %s. Connection error.", url)
            log.debug(e)
        except ConnectionError:
            log.error("Cannot connect to %s. Connection error.", url)
        except Exception as e:
            log.exception(e)

    def __process_data(self):
        if not self.__convert_queue.empty():
            url, converter, data = self.__convert_queue.get()
            converted_data = converter.convert(url, data)
            self.__gateway.send_to_storage(self.get_name(), converted_data)
            log.debug(converted_data)
        else:
            sleep(.01)

    def get_name(self):
        return self.name

    def is_connected(self):
        return self.__connected

    def open(self):
        self.__stopped = False
        self.start()

    def close(self):
        self.__stopped = True


