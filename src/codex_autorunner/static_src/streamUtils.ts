const decoder = new TextDecoder();

export function parseMaybeJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

export async function readEventStream(
  res: Response,
  handler: (event: string, data: string) => void
): Promise<void> {
  if (!res.body) throw new Error("Streaming not supported in this browser");

  const reader = res.body.getReader();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      if (!chunk.trim()) continue;
      let event = "message";
      const dataLines: string[] = [];
      chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
      });
      if (!dataLines.length) continue;
      handler(event, dataLines.join("\n"));
    }
  }
}
