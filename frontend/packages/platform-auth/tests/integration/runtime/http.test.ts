import { afterEach, describe, expect, it, vi } from "vitest";

import {
    ApiRequestError,
    describeUserError,
    errorCodeForStatus,
    isRetryableError,
    isVersionConflict,
    requestEnvelope,
    requestJson,
} from "../../../src/browser/http";

function jsonResponse(
    payload: unknown,
    init: { ok?: boolean; status?: number; statusText?: string } = {},
): Response {
    return {
        ok: init.ok ?? true,
        status: init.status ?? 200,
        statusText: init.statusText ?? "OK",
        json: async () => payload,
    } as Response;
}

afterEach(() => vi.unstubAllGlobals());

describe("requestEnvelope", () => {



    it("treats a success flag without data as a failure", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn().mockResolvedValue(jsonResponse({ success: false })),
        );
        await expect(requestEnvelope("/x")).rejects.toBeInstanceOf(
            ApiRequestError,
        );
    });
});

describe("requestJson", () => {


    it("wraps network failures as retryable status 0 errors", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
        );
        await expect(requestJson("/x")).rejects.toMatchObject({
            status: 0,
            retryable: true,
        });
    });
});

describe("errorCodeForStatus", () => {
    it.each([
        [401, "XJ-COM-002"],
        [403, "XJ-COM-003"],
        [404, "XJ-COM-004"],
        [409, "XJ-COM-005"],
        [422, "XJ-COM-001"],
        [500, "XJ-COM-006"],
        [503, "XJ-COM-006"],
        [418, "XJ-COM-001"],
    ])("maps HTTP %i to %s", (status, code) => {
        expect(errorCodeForStatus(status)).toBe(code);
    });
});

describe("isVersionConflict", () => {
    it("detects a 409 typed error even without a conflict message", () => {
        expect(isVersionConflict(new ApiRequestError("落后版本", 409))).toBe(
            true,
        );
    });

    it("detects conflict wording on a plain error", () => {
        expect(isVersionConflict(new Error("409 conflict"))).toBe(true);
        expect(isVersionConflict(new Error("版本冲突"))).toBe(true);
        expect(isVersionConflict(new Error("其他故障"))).toBe(false);
    });
});

describe("isRetryableError", () => {
    it("treats 5xx and status 0 typed errors as retryable", () => {
        expect(isRetryableError(new ApiRequestError("gateway", 502))).toBe(
            true,
        );
        expect(isRetryableError(new ApiRequestError("network", 0))).toBe(true);
    });

    it("recognizes outage wording on plain errors", () => {
        expect(
            isRetryableError(new Error("open-config failed: 502 Bad Gateway")),
        ).toBe(true);
        expect(isRetryableError(new Error("ODM upstream returned 500"))).toBe(
            true,
        );
        expect(isRetryableError(new Error("Forbidden"))).toBe(false);
    });
});
