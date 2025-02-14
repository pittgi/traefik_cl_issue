# -*- coding: utf-8 -*-
"""
This h2 server is a modified version of the official example server
asyncio-server.py. The modifications allow to process requests even though 
the client sent an RST_STREAM_FRAME (trying to abort a request). It also
ignores the value of the content-length header.

"""
import asyncio
import io
import ssl
import collections
from typing import List, Tuple

from hyperframe.exceptions import InvalidPaddingError

from h2.config import H2Configuration
from h2.connection import H2Connection, ConnectionInputs, AllowedStreamIDs
from h2.events import (
    ConnectionTerminated, DataReceived, RemoteSettingsChanged,
    RequestReceived, StreamEnded, StreamReset, WindowUpdated, Event
)
from h2.errors import ErrorCodes
from h2.exceptions import ProtocolError, StreamClosedError, StreamIDTooLowError
from h2.settings import SettingCodes
from h2.stream import H2Stream

from hyperframe.frame import Frame, DataFrame, RstStreamFrame


RequestData = collections.namedtuple('RequestData', ['headers', 'data'])


class ModifiedH2Stream(H2Stream):
    def __init__(self,
                 stream_id: int,
                 config: H2Configuration,
                 inbound_window_size: int,
                 outbound_window_size: int) -> None:
        super().__init__(stream_id, config, inbound_window_size, outbound_window_size)

    def _track_content_length(self, length: int, end_stream: bool) -> None:
        self._actual_content_length += length
        actual = self._actual_content_length
        expected = self._expected_content_length

        if expected is not None:
            if expected < actual:
                print(f"WARNING: Data expected length {expected} LESS then actual length {actual}")

            if end_stream and expected != actual:
                print(f"WARNING: Data expected length {expected} NOT MATCHING actual length {actual}")


class ModifiedH2Connection(H2Connection):
    def __init__(self, config, protocol):
        self.__protocol = protocol
        super().__init__(config)

    def _begin_new_stream(self, stream_id: int, allowed_ids: AllowedStreamIDs) -> H2Stream:
        """
        Initiate a new stream.

        .. versionchanged:: 2.0.0
           Removed this function from the public API.

        :param stream_id: The ID of the stream to open.
        :param allowed_ids: What kind of stream ID is allowed.
        """
        self.config.logger.debug(
            "Attempting to initiate stream ID %d", stream_id,
        )
        outbound = self._stream_id_is_outbound(stream_id)
        highest_stream_id = (
            self.highest_outbound_stream_id if outbound else
            self.highest_inbound_stream_id
        )

        if stream_id <= highest_stream_id:
            raise StreamIDTooLowError(stream_id, highest_stream_id)

        if (stream_id % 2) != int(allowed_ids):
            msg = "Invalid stream ID for peer."
            raise ProtocolError(msg)

        s = ModifiedH2Stream(
            stream_id,
            config=self.config,
            inbound_window_size=self.local_settings.initial_window_size,
            outbound_window_size=self.remote_settings.initial_window_size,
        )
        self.config.logger.debug("Stream ID %d created", stream_id)
        s.max_outbound_frame_size = self.max_outbound_frame_size

        self.streams[stream_id] = s
        self.config.logger.debug("Current streams: %s", self.streams.keys())

        if outbound:
            self.highest_outbound_stream_id = stream_id
        else:
            self.highest_inbound_stream_id = stream_id

        return s

    def _terminate_connection(self, error_code: ErrorCodes):
        print(f"Sending GOAWAY due to {error_code.name}")
        super().close_connection(error_code)
    
    def exception_response(self, events):
        print("Received malformed request")
        headers = None
        stream_id = None
        for event in events:
            if isinstance(event, RequestReceived):
                headers = collections.OrderedDict(event.headers)
                stream_id = event.stream_id
        try:
            if headers is None:
                print(f"ERROR: could not fetch headers: {events}")
                raise ProtocolError
            if stream_id is None:
                print(f"ERROR: could not fetch stream_id: {events}")
                raise ProtocolError
            self.__protocol.send_response(headers, stream_id)
        except ProtocolError as e:
            print(f"ERROR: {e}")
            self._terminate_connection(e.error_code)
            raise

    def _receive_data_frame(self, frame: DataFrame) -> tuple[list[Frame], list[Event]]:
        """
        Receive a data frame on the connection.
        """
        flow_controlled_length = frame.flow_controlled_length

        events = self.state_machine.process_input(
            ConnectionInputs.RECV_DATA,
        )
        self._inbound_flow_control_window_manager.window_consumed(
            flow_controlled_length,
        )

        try:
            stream = self._get_stream_by_id(frame.stream_id)
            frames, stream_events = stream.receive_data(
                frame.data,
                "END_STREAM" in frame.flags,
                flow_controlled_length,
            )
        except StreamClosedError as e:
            # This stream is either marked as CLOSED or already gone from our
            # internal state.
            return self._handle_data_on_closed_stream(events, e, frame)

        return frames, events + stream_events

    def _receive_frame(self, frame: Frame) -> list[Event]:
        """
        Handle a frame received on the connection.

        .. versionchanged:: 2.0.0
           Removed from the public API.
        """
        if isinstance(frame, RstStreamFrame):
            print(f"RST_FRAME received - error_code: {frame.error_code}")
        events: list[Event]
        self.config.logger.trace("Received frame: %s", repr(frame))
        try:
            # I don't love using __class__ here, maybe reconsider it.
            frames, events = self._frame_dispatch_table[frame.__class__](frame)
        except StreamClosedError as e:
            # If the stream was closed by RST_STREAM, we just send a RST_STREAM
            # to the remote peer. Otherwise, this is a connection error, and so
            # we will re-raise to trigger one.
            if self._stream_is_closed_by_reset(e.stream_id):
                f = RstStreamFrame(e.stream_id)
                f.error_code = e.error_code
                self._prepare_for_sending([f])
                events = e._events
            else:
                raise
        except StreamIDTooLowError as e:
            # The stream ID seems invalid. This may happen when the closed
            # stream has been cleaned up, or when the remote peer has opened a
            # new stream with a higher stream ID than this one, forcing it
            # closed implicitly.
            #
            # Check how the stream was closed: depending on the mechanism, it
            # is either a stream error or a connection error.
            if self._stream_is_closed_by_reset(e.stream_id):
                # Closed by RST_STREAM is a stream error.
                f = RstStreamFrame(e.stream_id)
                f.error_code = ErrorCodes.STREAM_CLOSED
                self._prepare_for_sending([f])
                events = []
            elif self._stream_is_closed_by_end(e.stream_id):
                # Closed by END_STREAM is a connection error.
                raise StreamClosedError(e.stream_id) from e
            else:
                # Closed implicitly, also a connection error, but of type
                # PROTOCOL_ERROR.
                raise
        else:
            self._prepare_for_sending(frames)

        return events

    def receive_data(self, data):
        """
        Pass some received HTTP/2 data to the connection for handling.

        :param data: The data received from the remote peer on the network.
        :type data: ``bytes``
        :returns: A list of events that the remote peer triggered by sending
            this data.
        """
        events = []
        self.incoming_buffer.add_data(data)
        self.incoming_buffer.max_frame_size = self.max_inbound_frame_size

        try:
            for frame in self.incoming_buffer:
                events.extend(self._receive_frame(frame))
        except InvalidPaddingError:
            self._terminate_connection(ErrorCodes.PROTOCOL_ERROR)
            raise ProtocolError("Received frame with invalid padding.")
        except ProtocolError as e:
            # For whatever reason, receiving the frame caused a protocol error.
            # We should prepare to emit a GoAway frame before throwing the
            # exception up further. No need for an event: the exception will
            # do fine.
            # self._terminate_connection(e.error_code)
            self.exception_response(events)
            # raise

        return events


class H2Protocol(asyncio.Protocol):
    def __init__(self):
        config = H2Configuration(client_side=False,
                                 header_encoding='utf-8',
                                 validate_inbound_headers=False,
                                 normalize_inbound_headers=False,
                                 validate_outbound_headers=False,
                                 normalize_outbound_headers=False)
        self.conn = ModifiedH2Connection(config=config, protocol=self)
        self.transport = None
        self.stream_data = {}
        self.flow_control_futures = {}

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.conn.initiate_connection()
        self.transport.write(self.conn.data_to_send())

    def connection_lost(self, exc):
        for future in self.flow_control_futures.values():
            future.cancel()
        self.flow_control_futures = {}

    def data_received(self, data: bytes):
        try:
            events = self.conn.receive_data(data)
        except ProtocolError as e:
            self.transport.write(self.conn.data_to_send())
            self.transport.close()
        else:
            self.transport.write(self.conn.data_to_send())
            for event in events:
                if isinstance(event, RequestReceived):
                    self.request_received(event.headers, event.stream_id)
                elif isinstance(event, DataReceived):
                    self.receive_data(
                        event.data, event.flow_controlled_length, event.stream_id
                    )
                elif isinstance(event, StreamEnded):
                    self.stream_complete(event.stream_id)
                elif isinstance(event, ConnectionTerminated):
                    self.transport.close()
                elif isinstance(event, StreamReset):
                    self.stream_reset(event.stream_id)
                elif isinstance(event, WindowUpdated):
                    self.window_updated(event.stream_id, event.delta)
                elif isinstance(event, RemoteSettingsChanged):
                    if SettingCodes.INITIAL_WINDOW_SIZE in event.changed_settings:
                        self.window_updated(None, 0)

                self.transport.write(self.conn.data_to_send())

    def request_received(self, headers: List[Tuple[str, str]], stream_id: int):
        headers = collections.OrderedDict(headers)
        method = headers[':method']

        # Store off the request data.
        request_data = RequestData(headers, io.BytesIO())
        self.stream_data[stream_id] = request_data

    def stream_complete(self, stream_id: int):
        """
        When a stream is complete, we can send our response.
        """

        try:
            request_data = self.stream_data[stream_id]
        except KeyError:
            # Just return, we probably 405'd this already
            return 
        self.send_response(request_data.headers, request_data.data.getvalue(), stream_id)

    def send_response(self, headers, body, stream_id: int):
        print("Request received")

        resp_data = ""
        for name, value in headers.items():
            resp_data += f"{name}:{value}"
        
        resp_data += b'\n' + body

        resp_data = resp_data.encode("utf8")

        response_headers = (
            (':status', '200'),
            ('content-type', 'text/plain'),
            ('content-length', str(len(resp_data))),
            ('server', 'asyncio-h2-mirror'),
        )
        self.conn.send_headers(stream_id, response_headers)
        asyncio.ensure_future(self.send_data(resp_data, stream_id))

    def receive_data(self, data: bytes, flow_controlled_length: int, stream_id: int):
        """
        We've received some data on a stream. If that stream is one we're
        expecting data on, save it off (and account for the received amount of
        data in flow control so that the client can send more data).
        Otherwise, reset the stream.
        """
        try:
            stream_data = self.stream_data[stream_id]
        except KeyError:
            self.conn.reset_stream(
                stream_id, error_code=ErrorCodes.PROTOCOL_ERROR
            )
        else:
            stream_data.data.write(data)
            self.conn.acknowledge_received_data(flow_controlled_length,
                                                stream_id)

    def stream_reset(self, stream_id):
        """
        A stream reset was sent. Stop sending data.
        """
        if stream_id in self.flow_control_futures:
            future = self.flow_control_futures.pop(stream_id)
            future.cancel()

    async def send_data(self, data, stream_id):
        """
        Send data according to the flow control rules.
        """
        while data:
            while self.conn.local_flow_control_window(stream_id) < 1:
                try:
                    await self.wait_for_flow_control(stream_id)
                except asyncio.CancelledError:
                    return

            chunk_size = min(
                self.conn.local_flow_control_window(stream_id),
                len(data),
                self.conn.max_outbound_frame_size,
            )

            try:
                self.conn.send_data(
                    stream_id,
                    data[:chunk_size],
                    end_stream=(chunk_size == len(data))
                )
            except (StreamClosedError, ProtocolError):
                # The stream got closed and we didn't get told. We're done
                # here.
                break

            self.transport.write(self.conn.data_to_send())
            data = data[chunk_size:]

    async def wait_for_flow_control(self, stream_id):
        """
        Waits for a Future that fires when the flow control window is opened.
        """
        f = asyncio.Future()
        self.flow_control_futures[stream_id] = f
        await f

    def window_updated(self, stream_id, delta):
        """
        A window update frame was received. Unblock some number of flow control
        Futures.
        """
        if stream_id and stream_id in self.flow_control_futures:
            f = self.flow_control_futures.pop(stream_id)
            f.set_result(delta)
        elif not stream_id:
            for f in self.flow_control_futures.values():
                f.set_result(delta)

            self.flow_control_futures = {}


if __name__ == "__main__":

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.options |= (
        ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_COMPRESSION
    )
    ssl_context.load_cert_chain(certfile="server.crt", keyfile="server.key")
    ssl_context.set_alpn_protocols(["h2"])

    loop = asyncio.get_event_loop()
    # Each client connection will create a new protocol instance
    coro = loop.create_server(H2Protocol, '127.0.0.1', 8443, ssl=ssl_context)
    server = loop.run_until_complete(coro)

    # Serve requests until Ctrl+C is pressed
    print('Serving on {}'.format(server.sockets[0].getsockname()))
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Close the server
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()