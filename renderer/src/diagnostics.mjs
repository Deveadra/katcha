// Provider diagnostics can contain proxy URLs wrapping presigned media URLs.
// Decode percent escapes before redaction, including malformed surrounding text.
// Never retain URL queries/fragments: credential names differ across providers.
export const redactDiagnostic = (value) => {
  let text = String(value?.message ?? value ?? 'Unknown renderer error');
  for (let depth = 0; depth < 16; depth += 1) {
    const decoded = text.replace(/(?:%[0-9a-f]{2})+/gi, (part) => {
      try { return decodeURIComponent(part); } catch {
        return part.replace(/%([0-9a-f]{2})/gi, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
      }
    });
    if (decoded === text) break;
    text = decoded;
  }
  // Also remove JSON-escaped slashes emitted by some SDKs.
  text = text.replace(/\\\//g, '/');
  text = text.replace(/https?:\/\/[^\s<>"'`]+/gi, (raw) => {
    try {
      const url = new URL(raw);
      return `${url.protocol}//${url.host}${url.pathname}${url.search || url.hash ? '?[redacted]' : ''}`;
    } catch { return '[redacted URL]'; }
  });
  // Catch detached query fragments and excessive nested encoding conservatively.
  text = text.replace(/\b(?:X-Amz-[a-z-]+|AWSAccessKeyId|Signature|SecurityToken|access_token|token|credential)(?:%25)*(?:%3d|=)[^\s&"'<>]*/gi, '[redacted credential]');
  return text;
};

export const sanitizedError = (error) => {
  // A fresh Error intentionally drops SDK causes, request objects and raw stacks.
  return new Error(redactDiagnostic(error));
};
