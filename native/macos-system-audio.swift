import CoreAudio
import CoreMedia
import Darwin
import Foundation
import ScreenCaptureKit

// このヘルパーは .audio のみ購読する。ディスプレイはSCContentFilterの指定に使う。
private let rate = 16_000
private let hostClock = CMClockGetHostTimeClock()
private func now() -> Double { CMClockGetTime(hostClock).seconds }
private struct CaptureFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
private func report(_ fields: [String: Any]) {
    guard var line = try? JSONSerialization.data(withJSONObject: fields) else { return }
    line.append(10)
    try? FileHandle.standardOutput.write(contentsOf: line)
}

// AVAudioFile の終了処理に依存せず、収録途中でも読み出せるWAVを作る。
private final class WaveTimeline {
    private let file: FileHandle
    private let silence = Data(repeating: 0, count: 32_000)
    private(set) var count = 0
    private static let maxFrames = (Int(UInt32.max) - 36) / 2

    init(path: String) throws {
        let descriptor = Darwin.open(path, O_CREAT | O_EXCL | O_WRONLY, 0o600)
        guard descriptor >= 0 else {
            throw CaptureFailure("システム音声の新規ファイルを作成できません（\(String(cString: strerror(errno)))）。")
        }
        file = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        try checkpoint()
    }

    func checkpoint() throws {
        var header = Data()
        func word<T: FixedWidthInteger>(_ x: T) {
            var le = x.littleEndian
            withUnsafeBytes(of: &le) { header.append(contentsOf: $0) }
        }
        let bytes = UInt32(count * 2)
        header.append(contentsOf: "RIFF".utf8)
        word(bytes + 36)
        header.append(contentsOf: "WAVEfmt ".utf8)
        word(UInt32(16)); word(UInt16(1)); word(UInt16(1))
        word(UInt32(rate)); word(UInt32(rate * 2)); word(UInt16(2)); word(UInt16(16))
        header.append(contentsOf: "data".utf8)
        word(bytes)
        try file.seek(toOffset: 0)
        try file.write(contentsOf: header)
        try file.seek(toOffset: UInt64(44 + count * 2))
    }

    func pad(to target: Int) throws {
        guard target <= Self.maxFrames else { throw CaptureFailure("システム音声のWAVが4GBの上限に達しました。") }
        while count < target {
            let frames = min(target - count, silence.count / 2)
            try file.write(contentsOf: silence.prefix(frames * 2))
            count += frames
        }
    }

    func append(_ pcm: [Int16], at first: Int) throws {
        guard first + pcm.count <= Self.maxFrames else { throw CaptureFailure("システム音声のWAVが4GBの上限に達しました。") }
        try pad(to: first)
        let skip = max(0, count - first)
        if skip < pcm.count {
            try pcm.withUnsafeBytes { bytes in
                try file.write(contentsOf: Data(bytes.dropFirst(skip * 2)))
            }
            count += pcm.count - skip
        }
    }

    func finish(at frames: Int) throws {
        // stopCapture の完了を待つ間に届いた、停止時刻より後のフレームを除く。
        if frames < count {
            count = max(0, frames)
            try file.truncate(atOffset: UInt64(44 + count * 2))
        } else {
            try pad(to: frames)
        }
        try checkpoint()
        try file.close()
    }
}

private func toPCM(_ value: Float) -> Int16 {
    guard value.isFinite else { return 0 }
    return Int16(max(-32768, min(32767, (value * 32768).rounded())))
}

private func samples(_ buffer: CMSampleBuffer) throws -> [Int16] {
    guard let format = buffer.formatDescription?.audioStreamBasicDescription,
          format.mFormatID == kAudioFormatLinearPCM,
          format.mSampleRate == Double(rate), format.mChannelsPerFrame == 1 else {
        throw CaptureFailure("システム音声の16kHzモノラル入力を初期化できません。")
    }
    return try buffer.withAudioBufferList { buffers, _ in
        guard let first = buffers.first, let ptr = first.mData,
              Int(first.mDataByteSize) >= buffer.numSamples * Int(format.mBytesPerFrame) else {
            throw CaptureFailure("システム音声のサンプルを読み出せません。")
        }
        if format.mFormatFlags & kAudioFormatFlagIsFloat != 0 && format.mBitsPerChannel == 32 {
            let floats = UnsafeBufferPointer(start: ptr.assumingMemoryBound(to: Float.self), count: buffer.numSamples)
            return floats.map(toPCM)
        }
        if format.mFormatFlags & kAudioFormatFlagIsSignedInteger != 0 && format.mBitsPerChannel == 16 {
            return Array(UnsafeBufferPointer(start: ptr.assumingMemoryBound(to: Int16.self), count: buffer.numSamples))
        }
        throw CaptureFailure("このMacのシステム音声のPCM形式を扱えません。")
    }
}

private final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    let queue = DispatchQueue(label: "localminutes.macos-audio")
    private let path: String
    private var stream: SCStream?
    private var writer: WaveTimeline?
    private var timer: DispatchSourceTimer?
    private var origin = 0.0
    private var stopTime: Double?
    private var peak = 0.0
    private var lastCheckpoint = 0.0
    private var finished = false
    init(path: String) { self.path = path }

    func start() {
        // stop要求は権限ダイアログ、コンテンツ一覧取得中にも受け付ける。
        DispatchQueue.global(qos: .utility).async {
            _ = readLine()
            self.queue.async { self.requestStop() }
        }
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
            self.queue.async {
                if let error { self.fail(error); return }
                guard let display = content?.displays.first else {
                    self.fail(CaptureFailure("使用できるディスプレイが見つかりません。")); return
                }
                do {
                    let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
                    let configuration = SCStreamConfiguration()
                    configuration.capturesAudio = true
                    configuration.channelCount = 1
                    configuration.sampleRate = rate
                    configuration.excludesCurrentProcessAudio = true
                    configuration.width = 2
                    configuration.height = 2
                    configuration.minimumFrameInterval = CMTime(seconds: 1, preferredTimescale: 1)
                    configuration.showsCursor = false
                    let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
                    self.stream = stream
                    try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: self.queue)
                    stream.startCapture { error in
                        self.queue.async {
                            if let error { self.fail(error); return }
                            do {
                                self.writer = try WaveTimeline(path: self.path)
                                self.origin = now()
                                report(["event": "ready", "startMonotonic": self.origin])
                                self.startMeter()
                            } catch { self.fail(error) }
                        }
                    }
                } catch { self.fail(error) }
            }
        }
    }

    private func startMeter() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + .milliseconds(250), repeating: .milliseconds(250))
        timer.setEventHandler {
            report(["event": "level", "peak": self.peak])
            self.peak = 0
            let time = now()
            if time - self.lastCheckpoint >= 1, let writer = self.writer {
                do {
                    // 出力音声が止まるとサンプルが届かない環境でも時刻を保つ。
                    // 数秒遅れの配送は、次のサンプル到着時にPTSで補正する。
                    try writer.pad(to: max(0, Int((time - self.origin - 3) * Double(rate))))
                    try writer.checkpoint()
                    self.lastCheckpoint = time
                } catch { self.fail(error) }
            }
        }
        self.timer = timer
        timer.resume()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of outputType: SCStreamOutputType) {
        guard outputType == .audio, let writer, sampleBuffer.isValid, sampleBuffer.numSamples > 0 else { return }
        do {
            let pts = sampleBuffer.presentationTimeStamp
            let clock = stream.synchronizationClock ?? hostClock
            let time = CMSyncConvertTime(pts, from: clock, to: hostClock).seconds
            guard time.isFinite, abs(time - now()) < 60 else {
                throw CaptureFailure("システム音声のタイムスタンプを同期できません。")
            }
            let pcm = try samples(sampleBuffer)
            try writer.append(pcm, at: Int(((time - origin) * Double(rate)).rounded()))
            for v in pcm { peak = max(peak, Double(abs(Int(v))) / 32768) }
        } catch { fail(error) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async {
            if self.stopTime == nil { self.fail(error) }
            else { self.complete() }
        }
    }

    private func requestStop() {
        guard stopTime == nil else { return }
        stopTime = now()
        timer?.cancel()
        guard let stream, writer != nil else { complete(); return }
        stream.stopCapture { error in
            self.queue.async {
                if let error { self.fail(error) }
                else { self.complete() }
            }
        }
        queue.asyncAfter(deadline: .now() + 3) { self.complete() }
    }

    private func fail(_ error: Error) { complete(error) }

    private func complete(_ failure: Error? = nil) {
        guard !finished else { return }
        finished = true
        timer?.cancel()
        var problem = failure
        if let writer {
            do { try writer.finish(at: max(0, Int((((stopTime ?? now()) - origin) * Double(rate)).rounded()))) }
            catch { problem = problem ?? error }
        }
        if let problem {
            let ns = problem as NSError
            let permission = ns.domain == SCStreamErrorDomain && ns.code == SCStreamError.userDeclined.rawValue
            let message = permission
                ? "システム音声の収録が許可されていません。システム設定の「プライバシーとセキュリティ」→「画面収録とシステムオーディオ録音」（または「画面収録」）で起動元を許可し、再起動してください。"
                : "システム音声を録音できません。\(problem.localizedDescription)"
            report(["event": "error", "message": message])
        }
        exit(problem == nil ? 0 : 1)
    }
}

guard CommandLine.arguments.count == 2 else {
    report(["event": "error", "message": "保存先のWAVファイルを指定してください。"])
    exit(2)
}
private let capture = Capture(path: CommandLine.arguments[1])
capture.start()
dispatchMain()
