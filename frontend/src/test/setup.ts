/**
 * Vitest global setup: extends `expect` with `@testing-library/jest-dom`'s
 * DOM matchers and cleans up the rendered tree between tests so component
 * state never leaks across test cases.
 */
import { cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
