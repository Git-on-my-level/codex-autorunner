/**
 * Golden wire fixtures: every sample payload in test/fixtures/<source>/ must
 * normalize to exactly the canonical event recorded beside it.
 */
import { describe, expect, test } from "bun:test";
import { parseEvent, type CarEvent } from "../../src/contract/events.ts";
import { normalizeAgentctl } from "../../src/ingest/agentctl.ts";
import { normalizeClaude } from "../../src/ingest/claude.ts";
import { normalizeMultica } from "../../src/ingest/multica.ts";
import { makeContext } from "../../src/ingest/normalize.ts";
import {
  expectation,
  FIXTURE_HOST,
  FIXTURE_NOW,
  loadFixtures,
  project,
  type Fixture,
} from "./fixtures.ts";

const ctx = () => makeContext(FIXTURE_NOW, FIXTURE_HOST);

type Normalizer = (fixture: Fixture) => { events: CarEvent[]; park?: boolean };

const SOURCES: { source: string; normalize: Normalizer }[] = [
  {
    source: "agentctl",
    normalize: (f) => ({ events: normalizeAgentctl(f.input, ctx()) }),
  },
  {
    source: "claude",
    normalize: (f) => {
      const result = normalizeClaude(f.input, ctx());
      return { events: [result.event], park: result.park };
    },
  },
  {
    source: "multica",
    normalize: (f) => ({ events: [normalizeMultica(f.input, ctx())] }),
  },
  {
    // The generic route runs no normalizer: the contract parser IS the adapter.
    source: "generic",
    normalize: (f) => ({ events: [parseEvent(f.input)] }),
  },
];

for (const { source, normalize } of SOURCES) {
  const fixtures = loadFixtures(source);

  describe(`golden fixtures: ${source}`, () => {
    test("at least three fixtures exist", () => {
      expect(fixtures.length).toBeGreaterThanOrEqual(3);
    });

    for (const fixture of fixtures) {
      test(`${fixture.file} — ${fixture.name}`, () => {
        const { events, park } = normalize(fixture);

        expect(events.length).toBe(fixture.expect.length);
        events.forEach((event, i) => {
          const expected = fixture.expect[i]!;
          expect(project(event, expected)).toEqual(expectation(expected));
        });

        if (fixture.expect_park !== undefined) {
          expect(park).toBe(fixture.expect_park);
        }
      });

      test(`${fixture.file} — normalizes deterministically`, () => {
        const first = normalize(fixture).events;
        const second = normalize(fixture).events;
        expect(second).toEqual(first);
      });
    }
  });
}
