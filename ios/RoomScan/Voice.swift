import AVFoundation
import SwiftUI

/// Says what to do next, also with the ring switch on silent: during a scan the phone often can't be read. One thing at a time, and
/// the same sentence is not repeated within `every` seconds.
enum Voice {
    private static let synth = AVSpeechSynthesizer()
    private static var last = ""
    private static var lastAt = Date.distantPast

    static func say(_ text: String, every: TimeInterval = 6) {
        guard UserDefaults.standard.object(forKey: "voiceGuide") as? Bool ?? true else { return }
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

/// The voice guidance on or off, on the screens that talk.
struct VoiceToggle: View {
    @AppStorage("voiceGuide") private var on = true

    var body: some View {
        Button {
            on.toggle()
            if !on { Voice.stop() }
        } label: {
            Image(systemName: on ? "speaker.wave.2.fill" : "speaker.slash.fill")
        }
        .buttonStyle(.bordered)
        .accessibilityLabel(on ? "Turn voice guidance off" : "Turn voice guidance on")
    }
}
