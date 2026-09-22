const assert = require("node:assert/strict");
const test = require("node:test");

const helpers = require(
  "../../src/standard_annotation_backend/static/token-management.js",
);

test("preset expirations produce UTC timestamps within backend bounds", () => {
  const dstBoundary = new Date("2026-03-10T19:00:00.000Z");
  assert.equal(
    helpers.expirationForChoice("week", dstBoundary),
    "2026-03-17T19:00:00.000Z",
  );
  assert.equal(
    helpers.expirationForChoice("month", new Date("2027-01-31T20:00:00.000Z")),
    "2027-02-28T20:00:00.000Z",
  );
  assert.equal(
    helpers.expirationForChoice("year", dstBoundary),
    "2027-03-10T19:00:00.000Z",
  );
});

test("custom expiration becomes an aware UTC timestamp and enforces one year", () => {
  const now = new Date("2026-09-16T00:00:00.000Z");
  assert.equal(
    helpers.expirationForChoice(
      "custom",
      now,
      "2026-10-01T00:00:00-07:00",
    ),
    "2026-10-01T07:00:00.000Z",
  );
  assert.throws(
    () =>
      helpers.expirationForChoice(
        "custom",
        now,
        "2027-09-16T00:00:00.001Z",
      ),
    /no more than one year/,
  );
});

test("token presentation uses human-readable metadata", () => {
  const token = {
    token_id: "11111111-2222-3333-4444-555566667777",
    name: "Notebook",
    role: "edit",
    scope: "group",
    group_id: "MGI",
    created_at: "2026-09-21T18:00:00Z",
    last_used_at: null,
    expires_at: "2026-10-21T18:00:00Z",
    revoked_at: null,
    assignment_is_active: true,
  };

  assert.equal(helpers.contextLabel(token), "Edit · Group · MGI");
  assert.equal(helpers.tokenIdentifier(token), "Notebook");
  assert.equal(
    helpers.tokenStatus(token, new Date("2026-09-22T00:00:00Z")),
    "Active",
  );
});
