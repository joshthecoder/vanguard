import _ from 'lodash';
import assert from 'assert';
import BufferOffset from 'buffer-offset';
import crc32 from 'buffer-crc32';
import Dissolve from 'dissolve';
import { sprintf } from 'sprintf-js';
import Struct from 'struct';
import { Transform } from 'stream';
import util from 'util';

/**
 * Vanguard binary protocol: Network byte order (big endian)
 *
 *                    ord('V') + ord('M')  ord('S') + ord('G')
 * bytes 0  .. 1     : 0xa39a (begin msg - uint16_t)
 * bytes 2  .. 5     : timestamp (uint32_t - seconds since epoch)
 * byte  6           : Message type (uint8_t)
 * byte  7           : Length of data segment (uint8_t)
 * bytes 8  .. 11    : CRC32 of data (uint32_t)
 * bytes 12 .. N     : Message data
 *
 *                    ord('V') + ord('E')  ord('N') + ord('D')
 * bytes N+1 .. N+2 : 0x9b92 (end msg - uint16_t)
*/

export const BEGIN          = 0xa39a;
export const END            = 0x9b92;
export const MARKER_SIZE    = 2;
export const TIMESTAMP_SIZE = 4;
export const MSG_TYPE_SIZE  = 1;
export const LENGTH_SIZE    = 1;
export const CRC32_SIZE     = 4;
export const HEADER_SIZE    = MARKER_SIZE + TIMESTAMP_SIZE + MSG_TYPE_SIZE +
                              LENGTH_SIZE + CRC32_SIZE;
export const DATA_BEGIN     = HEADER_SIZE;
export const ENVELOPE_SIZE  = HEADER_SIZE + MARKER_SIZE;

export const MSG_TYPE_UNKNOWN          = -1;
export const MSG_TYPE_LOCATION         = 0;
export const MSG_TYPE_TELEMETRY        = 1;
export const MSG_TYPE_PHOTO_DATA       = 3;
export const MSG_TYPE_PROGRAM_UPLOAD   = 4;
export const MSG_TYPE_PROGRAM_RESULT   = 5;
export const MSG_TYPE_START_PHOTO_DATA = 10;
export const MSG_TYPE_STOP_PHOTO_DATA  = 11;
export const MSG_TYPE_PING             = 12;
export const MSG_TYPE_PONG             = 13;

export const LOCATION_SIZE              = 26;
export const TELEMETRY_SIZE             = 20;
export const PHOTO_DATA_HEADER_SIZE     = 10;
export const PING_PONG_SIZE             = 4;
export const PROGRAM_UPLOAD_HEADER_SIZE = 10;
export const PROGRAM_RESULT_HEADER_SIZE = 11;

let Header = Struct().word16Ube('begin')
                     .word32Ube('timestamp')
                     .word8('type')
                     .word8('dataLength')
                     .word32Ube('crc32');

export class Parser extends Dissolve {
  constructor() {
    super();
    this.discard = '';
    this.loop(end => {
      this.parse();
    });
  }

  parse() {
    this.uint16be('begin').tap(() => {
      if (this.vars.begin !== BEGIN) {
        this.discard += String.fromCharCode(this.vars.begin >> 8);
        this.discard += String.fromCharCode(this.vars.begin & 0xff);
        return;
      }

      if (this.discard.length > 0) {
        console.log(this.discard);
        this.discard = '';
      }
      this.parseTimestamp();
    });
  }

  parseTimestamp() {
    this.uint32be('timestamp').tap(() => {
      this.parseMessage();
    });
  }

  parseMessage() {
    this.uint8('type').uint8('size').uint32be('crc32').tap(() => {
      this.tapMessage();
    });
  }

  tapMessage() {
    switch (this.vars.type) {
      case MSG_TYPE_LOCATION:
          assert.equal(this.vars.size, LOCATION_SIZE);
          this.doublebe('lat')
              .doublebe('lon')
              .floatbe('alt')
              .uint8('quality')
              .uint8('satellites')
              .floatbe('speed')
              .uint16be('end')
              .pushMessage();
          break;

      case MSG_TYPE_TELEMETRY:
          assert.equal(this.vars.size, TELEMETRY_SIZE);
          this.uint32be('uptime')
              .uint8('mode')
              .uint8('cpu')
              .uint16be('freeMem')
              .floatbe('intTemp')
              .floatbe('intHumidity')
              .floatbe('extTemp')
              .uint16be('end')
              .pushMessage();
          break;

      case MSG_TYPE_PHOTO_DATA:
          this.uint16be('index')
              .uint16be('chunk')
              .uint16be('chunkCount')
              .uint32be('fileSize')
              .buffer('data', this.vars.size - 10)
              .uint16be('end')
              .pushMessage();
          break;

      case MSG_TYPE_PING:
      case MSG_TYPE_PONG:
        this.uint32be('magic')
            .uint16be('end')
            .pushMessage();
        break;

      case MSG_TYPE_PROGRAM_UPLOAD:
        this.uint16be('index')
            .uint16be('chunk')
            .uint16be('chunkCount')
            .uint16be('programNameLen')
            .uint16be('programDataLen')
            .tap(() => {
              this.string('programName', this.vars.programNameLen)
                  .buffer('programData', this.vars.programDataLen)
            });
            this.pushMessage();
        break;

      case MSG_TYPE_PROGRAM_RESULT:
        this.uint16be('index')
            .uint16be('chunk')
            .uint16be('chunkCount')
            .uint16be('programNameLen')
            .uint16be('programDataLen')
            .int8('exitCode')
            .tap(() => {
              this.string('programName', this.vars.programNameLen)
                  .buffer('programData', this.vars.programDataLen)
            });
            this.pushMessage();
        break;
    }
  }

  pushMessage() {
    this.tap(() => {
      this.push(this.vars);
      this.vars = {};
    });
  }

  push(msg) {
    if (!msg) {
      return super.push(msg);
    }

    let type = msg.type;
    let data = _.omit(msg, 'type', 'begin', 'end');

    data.type = {
        [MSG_TYPE_LOCATION]: 'location',
        [MSG_TYPE_TELEMETRY]: 'telemetry',
        [MSG_TYPE_PHOTO_DATA]: 'photo-data',
        [MSG_TYPE_PING]: 'ping',
        [MSG_TYPE_PONG]: 'pong',
        [MSG_TYPE_PROGRAM_UPLOAD]: 'program-upload',
        [MSG_TYPE_PROGRAM_RESULT]: 'program-result'
    }[type];
    super.push(data);
  }
}

export class Message extends Buffer {
  constructor(dataLength, options) {
    super(dataLength + ENVELOPE_SIZE);

    options = _.defaults(options, {
      type: MSG_TYPE_UNKNOWN,
      crc32: 0,
      timestamp: Math.floor(Date.now() / 1000)
    });

    Header._setBuff(this);
    Header.set('begin', BEGIN);
    Header.set('type', options.type);
    Header.set('crc32', options.crc32);
    Header.set('dataLength', dataLength);

    this.setTimestamp(options.timestamp);
    this.writeUInt16BE(END, DATA_BEGIN + this.getDataLength());
  }

  getTimestamp() {
    Header._setBuff(this);
    return Header.fields.timestamp;
  }

  setTimestamp(value) {
    Header._setBuff(this);
    Header.fields.timestamp = value;
  }

  getCRC32() {
    Header._setBuff(this);
    return Header.fields.crc32;
  }

  setCRC32(value) {
    Header._setBuff(this);
    Header.fields.crc32 = value;
  }

  getData() {
    return this.slice(DATA_BEGIN, DATA_BEGIN + this.getDataLength());
  }

  setData(value) {
    value.copy(this, DATA_BEGIN, 0, this.getDataLength());
    this.setCRC32(crc32.unsigned(this.getData()));
  }

  getDataLength() {
    Header._setBuff(this);
    return Header.fields.dataLength;
  }

  static fromLocation(location) {
    let msg = new Message(LOCATION_SIZE, { type: MSG_TYPE_LOCATION });
    let data = new BufferOffset(LOCATION_SIZE);
    data.appendDoubleBE(location.lat);
    data.appendDoubleBE(location.lon);
    data.appendFloatBE(location.alt);
    data.appendUInt8(location.quality);
    data.appendUInt8(location.satellites);
    data.appendFloatBE(location.speed);
    msg.setData(data);
    return msg;
  }

  static fromTelemetry(telemetry) {
    let msg = new Message(TELEMETRY_SIZE, { type: MSG_TYPE_TELEMETRY });
    let data = new BufferOffset(TELEMETRY_SIZE);
    data.appendUInt32BE(telemetry.uptime);
    data.appendUInt8(0);
    data.appendUInt8(telemetry.cpu);
    data.appendUInt16BE(telemetry.freeMem);
    data.appendFloatBE(telemetry.intTemp);
    data.appendFloatBE(telemetry.intHumidity);
    data.appendFloatBE(telemetry.extTemp);
    msg.setData(data);
    return msg;
  }

  static fromPhotoData(photoData) {
    let dataLength = PHOTO_DATA_HEADER_SIZE + photoData.data.length;
    let msg = new Message(dataLength, { type: MSG_TYPE_PHOTO_DATA });
    let data = new BufferOffset(dataLength);
    data.appendUInt16BE(photoData.index);
    data.appendUInt16BE(photoData.chunk);
    data.appendUInt16BE(photoData.chunkCount);
    data.appendUInt32BE(photoData.fileSize);
    data.append(photoData.data);
    msg.setData(data);
    return msg;
  }

  static fromPingPong(type, pingData) {
    let msg = new Message(PING_PONG_SIZE, { type: type });
    let data = new BufferOffset(PING_PONG_SIZE);
    data.appendUInt32BE(pingData.magic);
    msg.setData(data);
    return msg;
  }

  static fromPing(data) {
    return Message.fromPingPong(MSG_TYPE_PING, data);
  }

  static fromPong(data) {
    return Message.fromPingPong(MSG_TYPE_PONG, data);
  }

  static fromProgramUpload(prgData){
    let dataLength = PROGRAM_UPLOAD_HEADER_SIZE + prgData.programName.length + prgData.programData.length;
    let msg = new Message(dataLength, {type: MSG_TYPE_PROGRAM_UPLOAD});
    let data = new BufferOffset(dataLength);
    let progName = prgData.programName;
    let progDataStr = prgData.programData;
    data.appendUInt16BE(prgData.index);
    data.appendUInt16BE(prgData.chunk);
    data.appendUInt16BE(prgData.chunkCount);
    data.appendUInt16BE(prgData.programNameLen);
    data.appendUInt16BE(prgData.programDataLen);
    data.append(new Buffer(progName));
    data.append(new Buffer(progDataStr));
    msg.setData(data);
    return msg;
  }

  static fromProgramResult(prgData){
    let dataLength = PROGRAM_RESULT_HEADER_SIZE + prgData.programName.length + prgData.programData.length;
    let msg = new Message(dataLength, {type: MSG_TYPE_PROGRAM_RESULT});
    let data = new BufferOffset(dataLength);
    data.appendUInt16BE(prgData.index);
    data.appendUInt16BE(prgData.chunk);
    data.appendUInt16BE(prgData.chunkCount);
    data.appendUInt16BE(prgData.programNameLen);
    data.appendUInt16BE(prgData.programDataLen);
    data.appendInt8(prgData.exitCode);
    data.append(new Buffer(prgData.programName));
    data.append(new Buffer(prgData.programData));
    msg.setData(data);
    return msg;
  }
}
