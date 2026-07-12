export function normalizeTextLines(value) {
  return String(value ?? "")
    .normalize("NFC")
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n")
    .replaceAll("　", " ")
    .split("\n")
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .filter(Boolean);
}

export function lineLengthsFromText(value) {
  return normalizeTextLines(value)
    .map((line) => Array.from(line).length)
    .join(",");
}

export function parseDialogueLineLengths(value) {
  const text = String(value ?? "").trim();
  if (!text) {
    return [];
  }
  return text
    .replace(/[;|/ ]/g, ",")
    .split(",")
    .filter((part) => part.trim())
    .map((part) => {
      const normalized = part.trim();
      if (!/^(?:0[xX][0-9a-fA-F]+|\d+)$/.test(normalized)) {
        throw new Error("invalid line length");
      }
      const length = Number(normalized);
      if (!Number.isInteger(length) || length <= 0 || length > 255) {
        throw new Error("invalid line length");
      }
      return length;
    });
}

export function previewLinesFromLengths(textValue, lengthsValue) {
  const lengths = parseDialogueLineLengths(lengthsValue);
  if (!lengths.length) {
    return { ok: true, lines: normalizeTextLines(textValue) };
  }
  const linearText = normalizeTextLines(textValue).join("");
  const characters = Array.from(linearText);
  const lines = [];
  let cursor = 0;
  for (const length of lengths) {
    lines.push(characters.slice(cursor, cursor + length).join(""));
    cursor += length;
  }
  if (cursor < characters.length) {
    lines.push(characters.slice(cursor).join(""));
  }
  return { ok: true, lines: lines.filter(Boolean) };
}
