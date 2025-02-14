import asyncio
import ssl
from collections import deque
from typing import Deque, Dict, Optional, cast
from urllib.parse import urlparse
from qh3.quic.packet import *
from qh3.asyncio.client import connect
from qh3.asyncio.protocol import QuicConnectionProtocol
from qh3.quic.configuration import QuicConfiguration
from qh3.quic.events import QuicEvent
from qh3.h3.connection import H3_ALPN, H3Connection
from qh3.h3.events import (DataReceived,
                           H3Event,
                           HeadersReceived,
                           PushPromiseReceived)


PATH_TO_TLS_SECRECTS = None


class URL:
    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        self.authority = parsed.netloc
        self.full_path = parsed.path or "/"
        if parsed.query:
            self.full_path += "?" + parsed.query
        self.scheme = parsed.scheme


class HttpRequest:
    def __init__(self,
                 method: str,
                 url: URL,
                 content: bytes = b"",
                 headers: Optional[Dict] = None) -> None:
        if headers is None:
            headers = {}
        self.content = content
        self.headers = headers
        self.method = method
        self.url = url


class HttpClient(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pushes: Dict[int, Deque[H3Event]] = {}
        self._request_events: Dict[int, Deque[H3Event]] = {}
        self._request_waiter: Dict[int, asyncio.Future[Deque[H3Event]]] = {}
        if self._quic.configuration.alpn_protocols[0].startswith("hq-"):
            print("ERROR: Missing python-module qh3.h0. Program exits.")
            exit(1)
        else:
            self._http = H3Connection(self._quic)

    def http_event_received(self, event: H3Event) -> None:
        if isinstance(event, (HeadersReceived, DataReceived)):
            stream_id = event.stream_id
            if stream_id in self._request_events:
                self._request_events[event.stream_id].append(event)
                if event.stream_ended:
                    req_waiter = self._request_waiter.pop(stream_id)
                    req_waiter.set_result(self._request_events.pop(stream_id))

            elif event.push_id in self.pushes:
                self.pushes[event.push_id].append(event)

        elif isinstance(event, PushPromiseReceived):
            self.pushes[event.push_id] = deque()
            self.pushes[event.push_id].append(event)

    def quic_event_received(self, event: QuicEvent) -> None:
        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self.http_event_received(http_event)


class H3ClientManager:
    def __init__(self, url, ca_certs, secrets_log):
        self.__configuration = QuicConfiguration(is_client=True,
                                                 alpn_protocols=H3_ALPN)
        self.__first_time = True
        self.__url = url
        self.__client = None

        if ca_certs is not None:
            self.__configuration.load_verify_locations(ca_certs)

        if secrets_log is not None:
            self.__configuration.secrets_log_file = open(secrets_log, "a")

        self.__configuration.verify_mode = ssl.CERT_NONE

    def connection_state(self):
        return self.__client._quic._state

    async def run_loop(self) -> None:
        # Parse URL
        parsed = urlparse(self.__url)
        if parsed.scheme != "https":
            self.__logger.critical("Only https:// URLs are supported")
            exit(-1)
        host = parsed.hostname
        if parsed.port is not None:
            port = parsed.port
        else:
            port = 443
        # Validate and process subsequent URLs
        _p = urlparse(self.__url)

        # Fill in if empty
        _scheme = _p.scheme or parsed.scheme
        _host = _p.hostname or host
        _port = _p.port or port

        if _scheme != parsed.scheme:
            self.__logger.critical("URL scheme doesn't match")
            exit(-1)
        if _host != host:
            self.__logger.critical("URL hostname doesn't match")
            exit(-1)
        if _port != port:
            self.__logger.critical("URL port doesn't match")
            exit(-1)

        # Reconstruct URL with new hostname and port
        _p = _p._replace(scheme=_scheme)
        _p = _p._replace(netloc="{}:{}".format(_host, _port))
        _p = urlparse(_p.geturl())
        self.__url = _p.geturl()

        while True:
            print("NEW CONNECTION")
            async with connect(host,
                               port,
                               configuration=self.__configuration,
                               create_protocol=HttpClient,
                               local_port=0) as client:
                self.__client = cast(HttpClient, client)
                try:
                    while True:
                        id = 1
                        resp = await asyncio.wait_for(self.perform_http_request(id), 1)
                        status_code = resp[0].headers[0][1]
                        print(status_code)
                        exit(-1) # remove if you want to send requests in inf loop
                except Exception as e:
                    print(f"Timeout!{id}")
                    exit(-1) # remove if you want to send requests in inf loop

    async def perform_http_request(self, id, headers=None, data=b'BBB') -> str:

        stream_id = self.__client._quic.get_next_available_stream_id()
        parsed_url = urlparse(self.__url)
        full_path = parsed_url.path

        if headers is None:
            headers = [
                    (b":method", b"POST"),
                    (b":scheme", b"https"),
                    (b":authority", parsed_url.netloc.encode()),
                    (b":path", full_path.encode()),
                    (b"content-length", b"4"),
                    (b"id", str(id).encode())
                ]

        self.__client._http.send_headers(
            stream_id=stream_id,
            headers=headers,
            end_stream=True if data is None else False,
        )

        send_data = data
        if isinstance(data, str):
            send_data = data.encode()
        if data is not None:
            self.__client._http.send_data(
                stream_id=stream_id, data=send_data, end_stream=True
            )

        self.__client.transmit()

        waiter = self.__client._loop.create_future()
        self.__client._request_events[stream_id] = deque()
        self.__client._request_waiter[stream_id] = waiter
        # Wait for response
        http_events = await asyncio.shield(waiter)
        return http_events

if __name__ == "__main__":
    manager = H3ClientManager("https://localhost:443/", None, PATH_TO_TLS_SECRECTS)
    asyncio.run(manager.run_loop())
