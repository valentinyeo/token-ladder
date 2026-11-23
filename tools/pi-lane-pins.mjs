import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const LANES = new URL("../ci/lanes.json", import.meta.url);

export function pinnedLanes(text = readFileSync(LANES, "utf8")) {
  return JSON.parse(text).filter((lane) => lane.gateway || lane.provider);
}

export function workerTags(cwd, brief = "") {
  const tags = ["caller=plan-pick-worker", `brief=${createHash("sha256").update(cwd).digest("hex").slice(0, 12)}`];
  const ticket = brief.match(/^Ticket[ \t]+([A-Z][A-Z0-9]{1,11}-[1-9][0-9]{0,9})\b/m);
  if (ticket) {
    tags.push(`ticket=${ticket[1]}`);
  }
  return tags;
}

export function pinned(payload, lanes, tags = ["caller=plan-pick-worker"]) {
  if (!payload || typeof payload !== "object") {
    return undefined;
  }
  const lane = lanes.find((candidate) => candidate.model === payload.model);
  if (!lane) {
    return undefined;
  }
  const next = { ...payload };
  if (lane.gateway) {
    next.providerOptions = { ...(payload.providerOptions || {}), gateway: { ...lane.gateway, tags } };
  }
  if (lane.provider) {
    next.provider = lane.provider;
  }
  return next;
}

export default function (pi) {
  const lanes = pinnedLanes();
  const cwd = process.cwd();
  let brief = "";
  try {
    brief = readFileSync(join(cwd, "brief.txt"), "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") {
      throw error;
    }
  }
  const tags = workerTags(cwd, brief);
  pi.on("before_provider_request", (event) => pinned(event.payload, lanes, tags));
}
