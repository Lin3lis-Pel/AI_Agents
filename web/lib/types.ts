// Names for the JSON the Python API returns. The shapes themselves are generated from the
// Pydantic models (see api/schema.py): run `npm run types` after changing a model.

import type { components } from "./api-schema";

type Schemas = components["schemas"];

export type Capabilities = Schemas["Capabilities"];
export type Severity = Schemas["Severity"];
export type Category = Schemas["Category"];
export type Finding = Schemas["Finding"];
export type CostLine = Schemas["CostLine"];
export type CostEstimate = Schemas["CostEstimate"];
export type Usage = Schemas["UsageSummary"];
export type ReviewReport = Schemas["ReviewReport"];

export type StepEvent = Schemas["StepEvent"];
export type ReportEvent = Schemas["ReportEvent"];
/** A problem the page found itself (the connection broke) has no id in the server's log. */
export type ProblemEvent = Omit<Schemas["ProblemEvent"], "review_id"> & { review_id?: string };
export type ReviewEvent = StepEvent | ReportEvent | ProblemEvent;

export type ReviewOptions = Schemas["ReviewOptions"];
export type ReviewRequest = Schemas["ReviewRequest"];
export type ProjectMemory = Schemas["ProjectConstraints"];
export type FeedbackAction = Schemas["FeedbackAction"];
export type FeedbackChoice = Schemas["FeedbackChoice"];
export type FinishResult = Schemas["FinishResult"];
export type SampleList = Schemas["SampleList"];
export type SampleFile = Schemas["SampleFile"];
