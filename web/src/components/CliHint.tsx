// A CLI command to copy when the browser path is not suitable (files over 256 MiB).
import { CopyButton } from "./bits";

export function CliHint({ text, command }: { text: string; command: string }) {
  return (
    <div className="cli-hint">
      <p>{text}</p>
      <pre>
        <code>{command}</code>
      </pre>
      <CopyButton value={command} label="Sao chép lệnh" />
    </div>
  );
}
