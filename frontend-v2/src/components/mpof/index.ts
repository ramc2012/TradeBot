/**
 * MP+OF visualization primitives — shared by the Institutional Convergence,
 * Auction Intelligence, Fractal and Commodity MP+OF lanes.
 *
 * Design rule for the module: every order-flow block must carry its source
 * honesty (REAL TICKS vs BAR PROXY) and its data timestamp. Components here
 * bake both in so desks can't accidentally render fabricated flow as real.
 */
export { ProfileLadder, type ProfileLadderProps } from "./ProfileLadder";
export { CvdPanel, type CvdPoint, type CvdDivergence } from "./CvdPanel";
export { FootprintGrid, type FootprintBar, type FootprintLevel } from "./FootprintGrid";
export { GateChips } from "./GateChips";
export { OfSourceBadge, classifyOfSource, type OfSourceKind } from "./OfSourceBadge";
