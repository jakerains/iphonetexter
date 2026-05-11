import Commander
import Foundation
import IMsgCore

enum NormalizeCommand {
  static let spec = CommandSpec(
    name: "normalize",
    abstract: "Normalize a phone number or email handle to canonical form",
    discussion: """
      Convert a phone number to E.164 format using the same logic that
      `imsg send` applies internally. Email handles are returned unchanged.
      Use --json for structured output that includes validity information,
      which is the recommended path for scripts.
      """,
    signature: CommandSignatures.withRuntimeFlags(
      CommandSignature(
        options: [
          .make(label: "to", names: [.long("to")], help: "phone number or email handle"),
          .make(
            label: "region", names: [.long("region")],
            help: "default region for phone normalization (default US)"),
        ]
      )
    ),
    usageExamples: [
      "imsg normalize --to \"(415) 555-1212\"",
      "imsg normalize --to \"+1 650-253-0000\" --json",
      "imsg normalize --to \"user@icloud.com\" --json",
    ]
  ) { values, runtime in
    try await run(values: values, runtime: runtime)
  }

  static func run(values: ParsedValues, runtime: RuntimeOptions) async throws {
    let input = try values.optionRequired("to")
    let region = values.option("region") ?? "US"

    let result = normalize(input: input, region: region)

    if runtime.jsonOutput {
      let payload = NormalizePayload(
        input: input,
        normalized: result.normalized,
        valid: result.isValid,
        kind: result.kind.rawValue
      )
      try JSONLines.print(payload)
    } else {
      StdoutWriter.writeLine(result.normalized)
    }
  }

  static func normalize(input: String, region: String) -> NormalizationResult {
    if input.contains("@") {
      return NormalizationResult(normalized: input, isValid: true, kind: .email)
    }
    let normalizer = PhoneNumberNormalizer()
    let normalized = normalizer.normalize(input, region: region)
    let isValid = isE164(normalized)
    return NormalizationResult(
      normalized: normalized,
      isValid: isValid,
      kind: isValid ? .phone : .unknown
    )
  }

  private static func isE164(_ value: String) -> Bool {
    guard value.hasPrefix("+"), value.count > 1 else { return false }
    return value.dropFirst().allSatisfy { $0.isNumber }
  }
}

struct NormalizationResult {
  let normalized: String
  let isValid: Bool
  let kind: HandleKind
}

enum HandleKind: String {
  case phone
  case email
  case unknown
}

private struct NormalizePayload: Encodable {
  let input: String
  let normalized: String
  let valid: Bool
  let kind: String
}
