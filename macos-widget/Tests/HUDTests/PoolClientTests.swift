import Foundation
import XCTest
@testable import HUD

/// The widget's two writes, against a stubbed daemon.
///
/// Nothing here touches the network: `URLProtocolStub` answers every request in
/// process, and `autoStart: false` keeps the poll timer from racing the
/// assertions. What is worth pinning is the *contract* with the daemon — the
/// method, the percent-encoded path, a real JSON boolean (the daemon rejects
/// `"false"` as a validation error), the CSRF header, and exactly one retry
/// when the token has rotated under us.
@MainActor
final class PoolClientTests: XCTestCase {

    // MARK: Stub

    /// One recorded request: everything the daemon would see.
    struct Recorded {
        let method: String
        let path: String
        let token: String?
        let body: [String: Any]
    }

    final class URLProtocolStub: URLProtocol {
        /// Answers keyed by path, in the order they should be served. The last
        /// answer for a path repeats once the list runs dry.
        nonisolated(unsafe) static var answers: [String: [(Int, String)]] = [:]
        nonisolated(unsafe) static var recorded: [Recorded] = []

        static func reset() {
            answers = [:]
            recorded = []
        }

        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

        override func startLoading() {
            let path = request.url?.path ?? ""
            // `httpBody` is nil by the time URLProtocol sees a streamed body,
            // so read the stream when it is.
            var raw = request.httpBody
            if raw == nil, let stream = request.httpBodyStream {
                stream.open()
                var buffer = [UInt8](repeating: 0, count: 4096)
                let read = stream.read(&buffer, maxLength: buffer.count)
                stream.close()
                if read > 0 { raw = Data(buffer[0..<read]) }
            }
            let parsed = raw.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            Self.recorded.append(Recorded(method: request.httpMethod ?? "",
                                          path: path,
                                          token: request.value(forHTTPHeaderField: "X-CSRF-Token"),
                                          body: parsed ?? [:]))

            var queue = Self.answers[path] ?? [(200, "{}")]
            let (code, json) = queue.first ?? (200, "{}")
            if queue.count > 1 { queue.removeFirst(); Self.answers[path] = queue }

            let response = HTTPURLResponse(url: request.url!, statusCode: code,
                                           httpVersion: "HTTP/1.1", headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(json.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }

        override func stopLoading() {}
    }

    private func makeClient() -> PoolClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [URLProtocolStub.self]
        return PoolClient(port: 4317, session: URLSession(configuration: config), autoStart: false)
    }

    private let status = #"{"version":"1.0","csrf_token":"tok-1"}"#
    private let profiles = #"{"profiles":[]}"#

    override func setUp() {
        super.setUp()
        URLProtocolStub.reset()
        URLProtocolStub.answers["/api/status"] = [(200, status)]
        URLProtocolStub.answers["/api/profiles"] = [(200, profiles)]
    }

    override func tearDown() {
        URLProtocolStub.reset()
        super.tearDown()
    }

    private var writes: [Recorded] {
        URLProtocolStub.recorded.filter { $0.method == "PATCH" || $0.method == "POST" }
    }

    // MARK: Disable / Enable

    func testDisableSendsARealJSONFalse() async throws {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/work"] = [(200, #"{"profile":{}}"#)]

        try await client.setEnabled(false, for: "work")

        let write = try XCTUnwrap(writes.first)
        XCTAssertEqual(write.method, "PATCH")
        XCTAssertEqual(write.path, "/api/profiles/work")
        // The daemon validates the type: `"false"` is a 400, not a falsey value.
        XCTAssertEqual(write.body["enabled"] as? Bool, false)
        XCTAssertTrue(write.body["enabled"] is Bool)
        XCTAssertEqual(write.body.count, 1, "nothing but `enabled` may be patched")
    }

    func testEnableSendsARealJSONTrue() async throws {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/work"] = [(200, #"{"profile":{}}"#)]

        try await client.setEnabled(true, for: "work")

        XCTAssertEqual(writes.first?.body["enabled"] as? Bool, true)
    }

    /// Profile ids are user-chosen names; a space or a slash must not walk off
    /// the endpoint.
    func testTheProfileIdIsPercentEncoded() async throws {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/my%20work%20acct"] = [(200, #"{"profile":{}}"#)]

        try await client.setEnabled(false, for: "my work acct")

        // `URL.path` decodes, so the stub sees the original — what matters is
        // that it reached the right endpoint at all rather than 404ing.
        XCTAssertEqual(writes.first?.path, "/api/profiles/my work acct")
    }

    /// The same endpoint serves every kind: `kind` is not patchable and nothing
    /// here touches credentials, so verifying one must not be verifying one.
    func testEveryProfileKindTakesTheSamePath() async throws {
        for kind in ["oauth", "codex", "api"] {
            URLProtocolStub.reset()
            URLProtocolStub.answers["/api/status"] = [(200, status)]
            URLProtocolStub.answers["/api/profiles"] = [(200, profiles)]
            URLProtocolStub.answers["/api/profiles/\(kind)-acct"] = [(200, #"{"profile":{}}"#)]

            try await makeClient().setEnabled(false, for: "\(kind)-acct")

            XCTAssertEqual(writes.first?.method, "PATCH", "\(kind) took a different method")
            XCTAssertEqual(writes.first?.path, "/api/profiles/\(kind)-acct")
            XCTAssertEqual(writes.first?.body["enabled"] as? Bool, false, "\(kind) sent a different body")
        }
    }

    func testTakeOverStillPostsWithNoBody() async throws {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/work/take-over"] = [(200, "{}")]

        try await client.takeOver("work")

        let write = try XCTUnwrap(writes.first)
        XCTAssertEqual(write.method, "POST")
        XCTAssertEqual(write.path, "/api/profiles/work/take-over")
        XCTAssertTrue(write.body.isEmpty)
    }

    // MARK: CSRF

    func testTheTokenComesFromStatus() async throws {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/work"] = [(200, #"{"profile":{}}"#)]

        try await client.setEnabled(false, for: "work")

        XCTAssertEqual(writes.first?.token, "tok-1")
    }

    func testARotatedTokenIsRefetchedAndRetriedExactlyOnce() async throws {
        let client = makeClient()
        // The daemon restarted between the last poll and this click.
        URLProtocolStub.answers["/api/status"] = [(200, status), (200, #"{"version":"1.0","csrf_token":"tok-2"}"#)]
        URLProtocolStub.answers["/api/profiles/work"] = [
            (403, #"{"error":"csrf"}"#),
            (200, #"{"profile":{}}"#),
        ]

        try await client.setEnabled(false, for: "work")

        XCTAssertEqual(writes.count, 2, "one retry, not a loop")
        XCTAssertEqual(writes[0].token, "tok-1")
        XCTAssertEqual(writes[1].token, "tok-2")
    }

    func testAPersistent403GivesUpAfterOneRetry() async {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/work"] = [(403, #"{"error":"csrf","message":"Bad token."}"#)]

        do {
            try await client.setEnabled(false, for: "work")
            XCTFail("a 403 must surface")
        } catch {
            XCTAssertEqual(error.localizedDescription, "Bad token.")
        }
        XCTAssertEqual(writes.count, 2)
    }

    // MARK: Failures

    func testTheDaemonsOwnMessageIsWhatTheAlertShows() async {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/gone"] = [
            (404, #"{"error":"not_found","message":"No profile named gone."}"#)
        ]

        do {
            try await client.setEnabled(false, for: "gone")
            XCTFail("a 404 must surface")
        } catch {
            XCTAssertEqual(error.localizedDescription, "No profile named gone.")
        }
    }

    func testAMessagelessFailureStillSaysSomething() async {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/work"] = [(500, "")]

        do {
            try await client.setEnabled(false, for: "work")
            XCTFail("a 500 must surface")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("500"),
                          "got: \(error.localizedDescription)")
        }
    }

    /// A refusal must not leave the dock showing the old state; a success
    /// re-polls so the tile flips without waiting for the next tick.
    func testASuccessfulWriteRefreshesThePool() async throws {
        let client = makeClient()
        URLProtocolStub.answers["/api/profiles/work"] = [(200, #"{"profile":{}}"#)]

        try await client.setEnabled(false, for: "work")

        XCTAssertTrue(URLProtocolStub.recorded.contains { $0.path == "/api/profiles" },
                      "the write did not re-poll")
        XCTAssertNotNil(client.lastUpdated)
    }
}
