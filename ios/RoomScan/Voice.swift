import AVFoundation

/// Says what to do next, also with the ring switch on silent: during a scan the phone often can't be read. One thing at a time, and
/// the same sentence is not repeated within `every` seconds.
enum Voice {
    private static let synth = AVSpeechSynthesizer()
    private static var last = ""
    private static var lastAt = Date.distantPast

    static func say(_ text: String, every: TimeInterval = 6) {
        guard !text.isEmpty, !synth.isSpeaking, text != last || Date().timeIntervalSince(lastAt) > every else { return }
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try? AVAudioSession.sharedInstance().setActive(true)
        last = text
        lastAt = Date()
        synth.speak(AVSpeechUtterance(string: text))
    }

    static func stop() {
        synth.stopSpeaking(at: .immediate)
        last = ""
    }
}
