import SwiftUI
import AVFoundation

/// Fixed phone, spinning plate: takes a full-resolution photo every few seconds while the part turns,
/// then the Object tab builds the model from the photos with Object Capture's photogrammetry.
final class TurntableCapture: NSObject, ObservableObject, AVCapturePhotoCaptureDelegate {
    let session = AVCaptureSession()
    private let output = AVCapturePhotoOutput()
    private var device: AVCaptureDevice?
    private var configured = false
    private var timer: Timer?
    private(set) var imagesDir: URL?
    @Published var count = 0
    @Published var running = false
    @Published var interval = 1.5      // seconds between photos
    @Published var status = ""
    private let maxPhotos = 120

    /// Start the camera preview (once).
    func prepare() {
        if !configured {
            configured = true
            session.beginConfiguration()
            session.sessionPreset = .photo
            if let cam = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
               let input = try? AVCaptureDeviceInput(device: cam), session.canAddInput(input) {
                session.addInput(input)
                device = cam
            }
            if session.canAddOutput(output) { session.addOutput(output) }
            output.maxPhotoQualityPrioritization = .quality
            session.commitConfiguration()
        }
        if !session.isRunning { DispatchQueue.global().async { self.session.startRunning() } }
    }

    func shutdown() {
        stop()
        if session.isRunning { DispatchQueue.global().async { self.session.stopRunning() } }
    }

    func start() {
        if imagesDir == nil {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("turntable-\(UUID().uuidString)")
            let images = base.appendingPathComponent("Images")
            try? FileManager.default.createDirectory(at: images, withIntermediateDirectories: true)
            imagesDir = images
            count = 0
        }
        lockCamera(true)   // focus, exposure and white balance must not drift while the part turns
        running = true
        status = ""
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in self?.shoot() }
        shoot()
    }

    func stop() {
        timer?.invalidate(); timer = nil
        if running { lockCamera(false) }
        running = false
    }

    /// Forget the photos, ready for the next part.
    func reset() {
        stop()
        imagesDir = nil
        count = 0
        status = ""
    }

    private func shoot() {
        guard count < maxPhotos else { stop(); status = "Reached \(maxPhotos) photos"; return }
        let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
        settings.photoQualityPrioritization = .balanced
        output.capturePhoto(with: settings, delegate: self)
    }

    private func lockCamera(_ lock: Bool) {
        guard let d = device, (try? d.lockForConfiguration()) != nil else { return }
        if lock {
            if d.isFocusModeSupported(.locked) { d.focusMode = .locked }
            if d.isExposureModeSupported(.locked) { d.exposureMode = .locked }
            if d.isWhiteBalanceModeSupported(.locked) { d.whiteBalanceMode = .locked }
        } else {
            if d.isFocusModeSupported(.continuousAutoFocus) { d.focusMode = .continuousAutoFocus }
            if d.isExposureModeSupported(.continuousAutoExposure) { d.exposureMode = .continuousAutoExposure }
            if d.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) { d.whiteBalanceMode = .continuousAutoWhiteBalance }
        }
        d.unlockForConfiguration()
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        guard let dir = imagesDir, error == nil, let data = photo.fileDataRepresentation() else {
            if let error { Reporter.report(tab: "turntable", message: "Photo failed", detail: "\(error)") }
            return
        }
        DispatchQueue.main.async {
            let n = self.count + 1
            let url = dir.appendingPathComponent(String(format: "IMG_%04d.jpg", n))
            if (try? data.write(to: url)) != nil { self.count = n }
        }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var layerView: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }

    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.layerView.session = session
        v.layerView.videoGravity = .resizeAspectFill
        return v
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}
}
