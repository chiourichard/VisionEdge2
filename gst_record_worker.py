"""Isolated OELinux hardware recorder. stdin = tightly packed BGR frames.

The CSV protocol matches the portable recorder: only closed MP4 fragments are
published. Native GStreamer failures stay outside the web/camera processes.
"""
import argparse
import csv
from fractions import Fraction
from pathlib import Path
import sys
import threading
import time


def read_frame(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = stream.read(size - len(chunks))
        if not data:
            if chunks:
                raise RuntimeError('Truncated BGR input frame')
            return None
        chunks.extend(data)
    return bytes(chunks)


def pack_bgr(data, width, height):
    # GstVideoInfo uses four-byte row alignment, unlike the packed pipe input.
    row = width * 3
    stride = (row + 3) & ~3
    if stride == row:
        return data
    out = bytearray(stride * height)
    for y in range(height):
        out[y * stride:y * stride + row] = data[y * row:(y + 1) * row]
    return bytes(out)


def build_pipeline(Gst, args):
    Gst.init(None)
    pipeline = Gst.Pipeline.new('annotated-hardware-recording')
    names = ('appsrc', 'videoconvert', 'capsfilter', 'v4l2h264enc', 'h264parse', 'splitmuxsink')
    elements = [Gst.ElementFactory.make(name, 'rec_' + name) for name in names]
    for name, element in zip(names, elements):
        if element is None:
            raise RuntimeError('Missing GStreamer element: ' + name)
    if Gst.ElementFactory.find('mp4mux') is None:
        raise RuntimeError('Missing GStreamer element: mp4mux')
    src, convert, caps, encoder, parser, sink = elements
    rate = Fraction(str(args.fps)).limit_denominator(1001)
    src.set_property('caps', Gst.Caps.from_string(
        f'video/x-raw,format=BGR,width={args.width},height={args.height},framerate={rate.numerator}/{rate.denominator}'))
    src.set_property('format', Gst.Format.TIME)
    src.set_property('is-live', True)
    src.set_property('do-timestamp', False)
    src.set_property('block', True)
    src.set_property('max-bytes', ((args.width * 3 + 3) & ~3) * args.height * 2)
    caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=NV12'))
    encoder.set_property('capture-io-mode', 0)
    encoder.set_property('output-io-mode', 0)
    controls = Gst.Structure.new_empty('controls')
    controls.set_value('video_bitrate', args.bitrate)
    controls.set_value('h264_i_frame_period', max(1, round(args.fps * 2)))
    encoder.set_property('extra-controls', controls)
    parser.set_property('config-interval', -1)
    sink.set_property('location', args.pattern)
    sink.set_property('max-size-time', int(args.segment * Gst.SECOND))
    sink.set_property('max-size-bytes', 0)
    sink.set_property('max-files', 0)
    sink.set_property('send-keyframe-requests', True)
    sink.set_property('async-finalize', True)
    sink.set_property('muxer-factory', 'mp4mux')
    for element in elements:
        pipeline.add(element)
    for left, right in zip(elements, elements[1:]):
        if not left.link(right):
            raise RuntimeError(f'Cannot link {left.get_name()} to {right.get_name()}')
    return pipeline, src


def run(args):
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    pipeline, src = build_pipeline(Gst, args)
    errors = []
    eof = threading.Event()
    eof_time = [None]
    def feed():
        index = 0
        try:
            while True:
                data = read_frame(sys.stdin.buffer, args.width * args.height * 3)
                if data is None:
                    break
                data = pack_bgr(data, args.width, args.height)
                buffer = Gst.Buffer.new_allocate(None, len(data), None)
                buffer.fill(0, data)
                buffer.pts = round(index * Gst.SECOND / args.fps)
                buffer.dts = Gst.CLOCK_TIME_NONE
                buffer.duration = round((index + 1) * Gst.SECOND / args.fps) - buffer.pts
                flow = src.emit('push-buffer', buffer)
                if flow != Gst.FlowReturn.OK:
                    raise RuntimeError('appsrc push-buffer failed: ' + str(flow))
                index += 1
            if index == 0:
                raise RuntimeError('No BGR frames received')
            if src.emit('end-of-stream') != Gst.FlowReturn.OK:
                raise RuntimeError('appsrc EOS failed')
        except Exception as exc:
            errors.append(str(exc))
        finally:
            eof_time[0] = time.monotonic()
            eof.set()

    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('Hardware recording pipeline failed to start')
        threading.Thread(target=feed, name='gst-bgr-feed', daemon=True).start()
        bus = pipeline.get_bus()
        opened = {}
        closed = set()
        eos = False
        with open(args.manifest, 'w', encoding='utf-8', newline='') as manifest:
            writer = csv.writer(manifest)
            while True:
                if errors:
                    raise RuntimeError(errors[0])
                if eof.is_set() and time.monotonic() - eof_time[0] > 10:
                    raise RuntimeError('Timed out finalizing MP4 fragments')
                msg = bus.timed_pop_filtered(100 * Gst.MSECOND,
                    Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.ELEMENT)
                if msg is None:
                    continue
                if msg.type == Gst.MessageType.ERROR:
                    error, debug = msg.parse_error()
                    raise RuntimeError(f'{error}: {debug}')
                if msg.type == Gst.MessageType.EOS:
                    eos = True
                if msg.type == Gst.MessageType.ELEMENT:
                    structure = msg.get_structure()
                    if structure is None:
                        continue
                    name = structure.get_name()
                    if name in ('splitmuxsink-fragment-opened', 'splitmuxsink-fragment-closed'):
                        location = str(structure.get_value('location'))
                        running = float(structure.get_value('running-time')) / Gst.SECOND
                        if name.endswith('-opened'):
                            opened[location] = running
                        else:
                            if location not in opened:
                                raise RuntimeError('Fragment closed without its opening timestamp')
                            writer.writerow((Path(location).name, opened.pop(location), running))
                            manifest.flush()
                            closed.add(location)
                if eos and not opened:
                    if not closed:
                        raise RuntimeError('No completed MP4 fragments')
                    return 0
    finally:
        pipeline.set_state(Gst.State.NULL)


def main():
    parser = argparse.ArgumentParser()
    for key in ('width', 'height', 'bitrate', 'segment'):
        parser.add_argument('--' + key, type=int, required=True)
    parser.add_argument('--fps', type=float, required=True)
    parser.add_argument('--pattern', required=True)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    if min(args.width, args.height, args.bitrate, args.segment, args.fps) <= 0:
        parser.error('Recording dimensions/rates must be positive')
    try:
        return run(args)
    except Exception as exc:
        print('GStreamer hardware recorder: ' + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
