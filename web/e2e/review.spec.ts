import { expect, test, type Page } from "@playwright/test";

const SAMPLE = "vulnerable/sql_open.tf";
const ADVICE = "High availability doubles the cost of this instance.";

async function reviewSample(page: Page, project: string) {
  await page.goto("/");
  await page.getByRole("textbox", { name: "Project", exact: true }).fill(project);
  await page.getByLabel(/start from an example/i).selectOption(SAMPLE);
  await expect(page.getByRole("textbox", { name: "Terraform" })).toHaveValue(
    /google_sql_database_instance/,
  );
  await page.getByRole("button", { name: "Review", exact: true }).click();
}

test("a review streams its steps, shows the report and remembers the feedback", async ({
  page,
}) => {
  await reviewSample(page, "e2e-feedback");

  // The steps arrive while the report is still being written: the response is streamed
  // all the way through the Next.js server, not sent at once at the end.
  await expect(page.getByText("Checked the security rules")).toBeVisible();
  await expect(page.getByText(/5 security finding\(s\)/)).toBeHidden();

  await expect(page.getByText(/5 security finding\(s\): 1 critical/)).toBeVisible();
  const security = page.getByRole("region", { name: /security findings/i });
  await expect(security.getByText("SQL-001")).toBeVisible();
  await expect(page.getByText(/231\.25/).first()).toBeVisible(); // hand-calculated cost
  await expect(page.getByRole("link", { name: /cloud\.google\.com/ }).first()).toBeVisible();

  // Only the suggestion can be rated. Security findings are not offered.
  const feedback = page.getByRole("region", { name: /your feedback/i });
  await expect(feedback.getByRole("group")).toHaveCount(1);
  await expect(feedback.getByText(ADVICE)).toBeVisible();
  await feedback.getByLabel(/not relevant/i).check();
  await feedback.getByRole("button", { name: /finish review/i }).click();
  await expect(page.getByText(/feedback saved/i)).toBeVisible();

  // The next review of the project knows, and all five security findings are still there.
  await reviewSample(page, "e2e-feedback");
  await expect(page.getByText(/5 security finding\(s\): 1 critical/)).toBeVisible();
  await expect(page.getByText("Lower priority because of your earlier feedback.")).toBeVisible();
});

test("input that is not Terraform is refused with a reason", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("textbox", { name: "Terraform" }).fill("please review my cloud");
  await page.getByRole("button", { name: "Review", exact: true }).click();

  await expect(page.getByText(/does not look like Terraform/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Review", exact: true })).toBeEnabled();
});

test("a connection that breaks during a review leaves the page usable", async ({ page }) => {
  await page.route("**/api/reviews", (route) =>
    route.fulfill({
      contentType: "application/x-ndjson",
      body: '{"type":"step","step":"parse"}\n', // and then nothing more
    }),
  );
  await reviewSample(page, "e2e-broken");

  await expect(page.getByText(/connection to the review service was lost/i)).toBeVisible();
  await expect(page.getByRole("button", { name: "Review", exact: true })).toBeEnabled();

  await page.unroute("**/api/reviews");
  await page.getByRole("button", { name: "Review", exact: true }).click();
  await expect(page.getByText(/5 security finding\(s\): 1 critical/)).toBeVisible();
});

test("the page says so when the API is not running", async ({ page }) => {
  await page.route("**/api/**", (route) => route.abort());
  await page.goto("/");

  await expect(page.getByText(/review service is not answering/i)).toBeVisible();
  await expect(page.getByText(/examples could not be loaded/i)).toBeVisible();
});
