import { pinned } from "../tools/pi-lane-pins.mjs";

export default function (pi) {
  const lane = JSON.parse(process.env.LADDER_AGENT_LANE);
  pi.on("before_provider_request", (event) => pinned(event.payload, [lane], ["caller=ladder-agent-ci"]));
}
