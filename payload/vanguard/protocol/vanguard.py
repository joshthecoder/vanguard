from collections import namedtuple
from cStringIO import StringIO
import logging
import struct
import time

from .. import hab_utils

''' Vanguard binary protocol: Network byte order (big endian)

                    ord('V') + ord('M')  ord('S') + ord('G')
bytes 0  .. 1     : 0xa39a (begin msg - uint16_t)
bytes 2  .. 5     : timestamp (uint32_t - seconds since epoch)
byte  6           : Message type (uint8_t)
byte  7           : Length of data segment (uint8_t)
bytes 8  .. 11    : CRC32 of data (uint32_t)
bytes 12 .. N     : Message data

                   ord('V') + ord('E')  ord('N') + ord('D')
bytes N+1 .. N+2 : 0x9b92 (end msg - uint16_t)
'''

class BadMarker(Exception):
    def __init__(self, got, expected):
        super(BadMarker, self).__init__('Bad marker. Got 0x%04X, expected 0x%04X' % (got, expected))

class BadMsgType(Exception):
    def __init__(self, msg_type):
        super(BadMsgType, self).__init__('Bad message type %d' % msg_type)

class BadChecksum(Exception):
    def __init__(self, got, expected):
        super(BadChecksum, self).__init__('Bad checksum. Got 0x%08X, expected 0x%08X' % (got, expected))

msg_types = {}
def msg_type(type_id):
    global msg_types
    def wrapper(msg_type):
        msg_type.TYPE = type_id
        msg_types[type_id] = msg_type
        return msg_type
    return wrapper

log = logging.getLogger('proto')

class Msg(object):
    TYPE = -1
    data_struct = None
    data_attrs  = None

    begin = 0xa39a # 'VMSG'
    begin_len = 2

    end = 0x9b92 # 'VEND'
    end_len = 2

    marker_struct = struct.Struct('!H')
    header_struct = struct.Struct('!LBBL')
    header_tuple  = namedtuple('Header', ['msg_timestamp', 'msg_type', 'msg_len', 'msg_crc32'])

    max_data_len = 255
    max_msg_len = begin_len + header_struct.size + end_len + max_data_len
    header_end = begin_len + header_struct.size

    @classmethod
    def validate_header(cls, buf):
        if not isinstance(buf, (str, buffer)):
            buf = buffer(buf)

        begin = cls.marker_struct.unpack_from(buf)
        if not begin or begin[0] != cls.begin:
            raise BadMarker(begin[0], cls.begin)

    @classmethod
    def from_header_buffer(cls, buf):
        global msg_types
        if not isinstance(buf, (str, buffer)):
            buf = buffer(buf)

        cls.validate_header(buf)
        header = cls.header_tuple._make(cls.header_struct.unpack_from(buf, cls.begin_len))
        if header.msg_type not in msg_types:
            raise BadMsgType(header.msg_type)

        msg = msg_types[header.msg_type](buf=buf)
        return msg

    @classmethod
    def from_data(cls, **kwargs):
        if not cls.data_struct:
            return cls(msg_data='')

        for key, val in cls.data_attrs:
            if key not in kwargs:
                kwargs[key] = val

        try:
            args = [kwargs.get(a[0], a[1]) for a in cls.data_attrs]
            return cls(msg_data=cls.data_struct.pack(*args))
        except:
            log.error('Invalid data for %s struct %s: %s', cls.__name__,
                                                           cls.data_struct.format,
                                                           str(args))
            return None

    def __init__(self, buf=None, msg_data=None):
        self._buffer = buf or bytearray(self.max_msg_len)
        self._buffer_ro = buffer(self._buffer)
        self._buffer_len = 0
        self.header_valid = False
        self.data_valid = False
        self.data_view = None

        self.data_tuple = None
        if self.data_attrs:
            self.data_tuple = namedtuple(self.__class__.__name__, (a[0] for a in self.data_attrs))

        if msg_data is not None:
            self.pack_data(msg_data)

    def __str__(self):
        header = self._get_header()
        return '%s[%d](type=%s, timestamp=%s, crc32=%x, %s)' % \
            (self.__class__.__name__,
             self._buffer_len,
             header.msg_type,
             header.msg_timestamp,
             header.msg_crc32,
             self.data_attr_str())

    def as_dict(self):
        header = self._get_header()
        d = dict(name=self.__class__.__name__,
                 len=self._buffer_len,
                 type=header.msg_type,
                 timestamp=header.msg_timestamp,
                 crc32=header.msg_crc32,
                 data={})

        if not self.data_attrs:
            return d

        for name, _ in self.data_attrs:
            d['data'][name] = getattr(self, name)

        return d

    def data_attr_str(self):
        if not self.data_attrs:
            return ''

        attr_strs = []
        for name, _ in self.data_attrs:
            attr_strs.append('%s=%s' % (name, repr(getattr(self, name))))

        return ', '.join(attr_strs)

    def data_str(self):
        return ' '.join(['%x' % ord(c) for c in self._buffer[:self._buffer_len]])

    def pack_data(self, msg_data):
        msg_data = msg_data or ''
        self.marker_struct.pack_into(self._buffer, 0, self.begin)
        self.header_struct.pack_into(self._buffer, self.begin_len,
                                     time.time(),
                                     self.TYPE,
                                     len(msg_data),
                                     hab_utils.crc32(msg_data))

        data_end = self.header_end + len(msg_data)
        self._buffer[self.header_end:data_end] = msg_data
        self.marker_struct.pack_into(self._buffer, data_end, self.end)
        self._get_header()
        self.validate_data()

    def as_dict(self):
        d = {}
        if self.data_attrs:
            for name, _ in self.data_attrs:
                d[name] = getattr(self, name)
        return d

    def as_buffer(self):
        return buffer(self._buffer, 0, self._buffer_len)

    def _get_header(self, check_begin=True):
        global msg_types
        if check_begin and not self.header_valid:
            self.validate_header(self._buffer)
            values = self.header_struct.unpack_from(self._buffer_ro, self.begin_len)
            header = self.header_tuple._make(values)
            if header.msg_type not in msg_types:
                raise BadMsgType(header.msg_type)

            self.header = header
            self.header_valid = True

        return self.header

    def _get_data(self):
        if not self.data_valid:
            self.validate_data()
            self.data_valid = True

        return self.data_view

    def __getattr__(self, attr):
        attrs = self.data_tuple._make(self.data_struct.unpack_from(self.msg_data))
        return getattr(attrs, attr)

    def validate_data(self):
        msg_timestamp, msg_type, msg_len, msg_crc32 = self._get_header()
        self.data_view = buffer(self._buffer, self.header_end, msg_len)
        crc32 = hab_utils.crc32(self.data_view)
        if crc32 != msg_crc32:
            raise BadChecksum(crc32, msg_crc32)

        data_len = self.header_end + msg_len
        self._buffer_len = data_len + self.end_len
        end = self.marker_struct.unpack_from(self._buffer_ro, data_len)
        if not end or end[0] != self.end:
            raise BadMarker(end[0], self.end)

    @property
    def msg_header(self):
        return self._get_header()

    @property
    def msg_timestamp(self):
        return self.msg_header.msg_timestamp

    @property
    def msg_type(self):
        return self.msg_header.msg_type

    @property
    def msg_len(self):
        return self.msg_header.msg_len

    @property
    def msg_crc32(self):
        return self.msg_header.msg_crc32

    @property
    def msg_data(self):
        return self._get_data()

@msg_type(0)
class LocationMsg(Msg):
    data_struct = struct.Struct('!ddfBBf')
    data_attrs  = (('latitude', 0), ('longitude', 0), ('altitude', 0),
                   ('quality', 0), ('satellites', 0), ('speed', 0))

@msg_type(1)
class TelemetryMsg(Msg):
    modes = ('preflight', 'ascent', 'descent', 'landed')
    mode_preflight  = 0
    mode_ascent     = 1
    mode_descent    = 2
    mode_landed     = 3
    cpu_unknown     = 127
    temp_unknown    = -999.999

    data_struct = struct.Struct('!LBBHfff')
    data_attrs  = (('uptime', 0), ('mode', mode_preflight), ('cpu_usage', cpu_unknown),
                   ('free_mem', 0), ('int_temperature', temp_unknown), ('int_humidity', temp_unknown),
                   ('ext_temperature', temp_unknown))

@msg_type(3)
class PhotoDataMsg(Msg):
    data_struct = struct.Struct('!HHHL')
    data_attrs = (('index', 0), ('chunk', 0), ('chunk_count', 0),
                  ('file_size', 0))

    @classmethod
    def from_data(cls, index=0, chunk=0, chunk_count=0, file_size=0, photo_data=''):
        header = cls.data_struct.pack(index, chunk, chunk_count, file_size)
        return cls(msg_data=header+photo_data)

    @property
    def photo_data(self):
        data = self.msg_data
        return buffer(self.msg_data, self.data_struct.size,
                      self.msg_len - self.data_struct.size)

@msg_type(4)
class ProgramUploadMsg(Msg):
    data_struct = struct.Struct('!HHHHH')
    data_attrs = (('index', 0), ('chunk', 0), ('chunk_count', 0), ('program_name_len', 0), ('program_data_len', 0))

    @classmethod
    def from_data(cls, index=0, chunk=0, chunk_count=0, program_name_len=0, program_data_len=0, program_name='', program_upload_data=''):
        header = cls.data_struct.pack(index, chunk, chunk_count, program_name_len, program_data_len)
        return cls(msg_data=header+program_name+program_upload_data)

    @property
    def message_data(self):
        return buffer(self.msg_data)

@msg_type(5)
class ProgramResultMsg(Msg):
    data_struct = struct.Struct('!HHHHHb') 
    data_attrs = (('index', 0), ('chunk', 0), ('chunk_count', 0), ('program_name_len', 0),
                  ('program_data_len', 0), ('exit_code',0))

    @classmethod
    def max_data_len(cls, program_name):
        return 255 - (cls.data_struct.size + len(program_name))

    @classmethod
    def from_data(cls, index=0, chunk=0, chunk_count=0, program_name_length=0, program_data_length=0, exit_code=0, program_name='', program_output_data=''):
        header = cls.data_struct.pack(index, chunk, chunk_count, program_name_length, program_data_length, exit_code)
        return cls(msg_data=header+program_name+program_output_data)

@msg_type(10)
class StartPhotoDataMsg(Msg):
    data_struct = struct.Struct('!H')
    data_attrs = (('index', 0), )

@msg_type(11)
class StopPhotoDataMsg(Msg):
    pass

@msg_type(12)
class PingMsg(Msg):
    data_struct = struct.Struct('!L')
    data_attrs = (('magic', 0), )

@msg_type(13)
class PongMsg(PingMsg):
    pass

class MsgReader(object):
    state_header = 0
    state_data   = 1
    state_end    = 2

    def __init__(self):
        self.log = logging.getLogger('msg_reader')
        self.buffer = bytearray(Msg.max_msg_len)
        self.reset()

    def reset(self):
        self.state = self.state_header
        self.msg = None
        self.index = 0

    def update(self, data):
        self.read(StringIO(data), eof_reset=False)
        if self.state == self.state_end:
            return self.msg
        return None

    def read(self, f, eof_reset=True):
        while self.state != self.state_end:
            b = f.read(1)
            if b == '':
                return None

            self.buffer[self.index] = b
            self.index += 1

            if self.state == self.state_header:
                self.handle_header_byte()
            elif self.state == self.state_data:
                self.handle_data_byte()

        if eof_reset:
            self.state = self.state_header
            self.index = 0

        return self.msg

    def handle_header_byte(self):
        if self.index != Msg.header_end:
            return

        try:
            self.msg = Msg.from_header_buffer(self.buffer)
            self.state = self.state_data

            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug('HEADER: %s[%d] = %s', self.msg.__class__.__name__,
                                                      self.msg.msg_len,
                                                      ' '.join(('%x' % c for c in self.buffer[:Msg.header_end])))
        except BadMarker, e:
            self.log.warn('Bad start marker, discarding %d out of sync bytes: %s', self.index,
                          ' '.join(('%x' % c for c in self.buffer[:Msg.header_end])))
            self.index = 0
            raise

    def handle_data_byte(self):
        if self.index != Msg.header_end + self.msg.msg_len + Msg.end_len:
            return

        try:
            self.msg.validate_data()
            self.state = self.state_end
        except (BadMarker, BadChecksum) as e:
            self.log.warn('%s, discarding %d out of sync bytes',
                          e.__class__.__name__, self.index)
            self.index = 0
            self.state = self.state_header
            raise

class VanguardProtocol(object):
    def __init__(self, **kwargs):
        pass

    def format_location(self, lat=0.0, lon=0.0, alt=0.0, speed=0, **kwargs):
        msg = LocationMsg.from_data(latitude=lat,
                                    longitude=lon,
                                    altitude=alt,
                                    speed=speed)
        return msg.as_buffer()

    def format_telemetry(self, **kwargs):
        msg = TelemetryMsg.from_data(
            uptime=int(kwargs.get('uptime', 0)),
            mode=0,
            cpu_usage=int(kwargs.get('cpu_usage', 0)),
            free_mem=int(kwargs.get('free_mem', 0) / 1024),
            int_temperature=kwargs.get('int_temp', 0),
            int_humidity=0,
            ext_temperature=kwargs.get('ext_temp', 0))
        return msg.as_buffer()

    def format_pong(self, magic, **kwargs):
        msg = PongMsg.from_data(magic=magic)
        return msg.as_buffer()

    def format_ProgramResultMsg(self, **kwargs):
        msg = ProgramResultMsg.from_data(index=kwargs.get('index'),
                                         chunk=kwargs.get('chunk'),
                                         chunk_count=kwargs.get('chunk_count'),
                                         exit_code=kwargs.get('exit_code'),
                                         program_name_length=(kwargs.get('program_name_length')),
                                         program_data_length=(kwargs.get('program_data_length')),
                                         program_name=(kwargs.get('program_name')),
                                         program_output_data=(kwargs.get('program_output_data')))
        return msg.as_buffer()

    def format_packet(self, data):
        return data

    def read_message(self, f):
        return MsgReader().read(f)
