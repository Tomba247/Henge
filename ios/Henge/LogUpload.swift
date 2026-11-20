import Foundation

/// Henge's log sent to a drop its user names, a piece a minute while Henge is open, so someone helping them can read
/// it without the phone being touched (leaving Henge to share a file can end a running Mac). Off unless Settings >
/// Engine options has a line HENGE_LOG_URL=https://... (EngineTuning.apply hands it here); the drop takes each piece
/// as POST <url>/<name>?off=<byte offset>. Only this launch's log goes, from its start.
enum LogUpload {
    private static let queue = DispatchQueue(label: "henge.log-upload", qos: .utility)
    private static var timer: DispatchSourceTimer?
    private static var base: URL?
    private static var sent: UInt64 = 0
    private static var busy = false
    private static let piece = 512 << 10
    /// One name a launch: henge-<UTC date and time>.log
    private static let name: String = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyyMMdd-HHmmss"
        return "henge-\(f.string(from: Date())).log"
    }()

    /// The drop's address, or nil (or anything but an https address) for off.
    static func configure(_ text: String?) {
        let url = text.flatMap { URL(string: $0.trimmingCharacters(in: .whitespaces)) }
        queue.async {
            guard let url, url.scheme == "https", url.host != nil else {
                if timer != nil { HengeLog.log("log-upload", "off") }
                timer?.cancel()
                timer = nil
                base = nil
                return
            }
            base = url
            if timer != nil { return }
            let t = DispatchSource.makeTimerSource(queue: queue)
            t.schedule(deadline: .now() + 5, repeating: 60)
            t.setEventHandler { step() }
            t.resume()
            timer = t
            HengeLog.log("log-upload", "on: this log goes to \(url.host ?? "the drop") as \(name), a piece a minute")
        }
    }

    /// On the queue: the log's bytes past what the drop has, one piece; then the next if more are waiting.
    private static func step() {
        guard let base, !busy else { return }
        guard let file = try? FileHandle(forReadingFrom: HengeLog.logFileURL) else { return }
        defer { try? file.close() }
        guard (try? file.seek(toOffset: sent)) != nil, let data = try? file.read(upToCount: piece), !data.isEmpty else { return }
        guard var parts = URLComponents(url: base.appendingPathComponent(name), resolvingAgainstBaseURL: false) else { return }
        parts.queryItems = [URLQueryItem(name: "off", value: String(sent))]
        guard let url = parts.url else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("text/plain; charset=utf-8", forHTTPHeaderField: "Content-Type")
        busy = true
        let at = sent
        URLSession.shared.uploadTask(with: request, from: data) { _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            queue.async {
                busy = false
                guard ok, sent == at else { return } // not taken: the same bytes go again next minute
                sent = at + UInt64(data.count)
                if data.count == piece { step() }
            }
        }.resume()
    }
}
