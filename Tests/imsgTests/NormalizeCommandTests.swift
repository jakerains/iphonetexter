import Foundation
import Testing

@testable import imsg

@Test
func normalizeCommandFormatsPhoneNumber() {
  let result = NormalizeCommand.normalize(input: "(415) 555-1212", region: "US")
  #expect(result.normalized == "+14155551212")
  #expect(result.isValid == true)
  #expect(result.kind == .phone)
}

@Test
func normalizeCommandPassesThroughExistingE164() {
  let result = NormalizeCommand.normalize(input: "+16502530000", region: "US")
  #expect(result.normalized == "+16502530000")
  #expect(result.isValid == true)
  #expect(result.kind == .phone)
}

@Test
func normalizeCommandTreatsEmailAsValid() {
  let result = NormalizeCommand.normalize(input: "user@icloud.com", region: "US")
  #expect(result.normalized == "user@icloud.com")
  #expect(result.isValid == true)
  #expect(result.kind == .email)
}

@Test
func normalizeCommandFlagsUnparseableAsInvalid() {
  let result = NormalizeCommand.normalize(input: "not-a-number", region: "US")
  #expect(result.normalized == "not-a-number")
  #expect(result.isValid == false)
  #expect(result.kind == .unknown)
}

@Test
func normalizeCommandPlainOutputWritesNormalizedLine() async throws {
  let captured = await StdoutCapture.capture {
    let router = CommandRouter()
    return await router.run(argv: ["imsg", "normalize", "--to", "(415) 555-1212"])
  }
  #expect(captured.value == 0)
  #expect(captured.output.trimmingCharacters(in: .whitespacesAndNewlines) == "+14155551212")
}

@Test
func normalizeCommandJSONOutputIncludesValidity() async throws {
  let captured = await StdoutCapture.capture {
    let router = CommandRouter()
    return await router.run(argv: ["imsg", "normalize", "--to", "garbage", "--json"])
  }
  #expect(captured.value == 0)
  let line = captured.output.trimmingCharacters(in: .whitespacesAndNewlines)
  let data = try #require(line.data(using: .utf8))
  let decoded = try JSONSerialization.jsonObject(with: data) as? [String: Any]
  #expect(decoded?["input"] as? String == "garbage")
  #expect(decoded?["normalized"] as? String == "garbage")
  #expect(decoded?["valid"] as? Bool == false)
  #expect(decoded?["kind"] as? String == "unknown")
}

@Test
func normalizeCommandRegisteredInRouter() {
  let router = CommandRouter()
  #expect(router.specs.contains { $0.name == "normalize" })
}
