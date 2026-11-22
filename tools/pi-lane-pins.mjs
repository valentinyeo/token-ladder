import { readFileSync } from "node:fs";

const LANES = new URL("../ci/lanes.json", import.meta.url);

export function pinnedLanes(text = readFileSync(LANES, "utf8")) {
  return JSON.parse(text).filter((lane) => lane.gateway || lane.provider);
}

export function pinned(payload, lanes) {
  if (!payload || typeof payload !== "object") {
    return undefined;
  }
  const lane = lanes.find((candidate) => candidate.model === payload.model);
  if (!lane) {
    return undefined;
  }
  const next = { ...payload };
  if (lane.gateway) {
    next.providerOptions = { ...(payload.providerOptions || {}), gateway: lane.gateway };
  }
  if (lane.provider) {
    next.provider = lane.provider;
  }
  return next;
}

export default function (pi) {
  const lanes = pinnedLanes();
  pi.on("before_provider_request", (event) => pinned(event.payload, lanes));
}
