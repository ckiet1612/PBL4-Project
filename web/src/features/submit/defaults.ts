// UX-A04: defaults that fit all three v1 templates. The server fills nothing in (strict JobSpec).
import type { SubmitDraft } from "./spec";

export const DEFAULT_DRAFT: Omit<SubmitDraft, "inputArtifactId" | "modelArtifactId" | "params"> = {
  cores: "1",
  memoryGib: "1",
  priority: "1",
  runtimeLimit: "300",
  checkpointInterval: "30",
};
