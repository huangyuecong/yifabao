import axios, {
    type AxiosAdapter,
    type AxiosInstance,
    type AxiosRequestConfig,
} from "axios";

export interface HttpOptions {
    apiBase: string;
    apiPrefix?: string;
    userId?: string;
}

export function createHttp({ apiBase, userId }: HttpOptions): AxiosInstance {
    return axios.create({
        baseURL: apiBase,
        withCredentials: true,
        headers: {
            ...(userId ? { "X-Yifabao-User-Id": userId } : {}),
        },
    });
}

export class ApiRequestError extends Error {
    readonly status: number;
    readonly code: string | undefined;
    readonly retryable: boolean | undefined;
    readonly rawMessage: string;

    constructor(
        message: string,
        status: number,
        code?: string,
        retryable?: boolean,
    ) {
        super(publicApiErrorMessage(message, status));
        this.name = "ApiRequestError";
        this.rawMessage = message;
        this.status = status;
        this.code = code;
        this.retryable = retryable;
    }
}

const STATUS_ERROR_CODES: Record<number, string> = {
    401: "XJ-COM-002",
    403: "XJ-COM-003",
    404: "XJ-COM-004",
    409: "XJ-COM-005",
    422: "XJ-COM-001",
};

export function errorCodeForStatus(status: number): string {
    if (status >= 500) return "XJ-COM-006";
    return STATUS_ERROR_CODES[status] ?? "XJ-COM-001";
}

export function describeError(error: unknown): string {
    if (error instanceof ApiRequestError) return error.rawMessage;
    if (axios.isAxiosError(error)) {
        const status = error.response?.status ?? 0;
        const data = error.response?.data;
        return `API ${status}: ${typeof data === "string" ? data : error.message}`;
    }
    return error instanceof Error ? error.message : String(error);
}

const VERSION_CONFLICT_RE = /409|conflict|版本冲突|version conflict/i;
const RETRYABLE_ERROR_RE =
    /50[0-9]|internal server error|failed to fetch|load failed|networkerror|network error|fetch failed|bad gateway|service unavailable|gateway|timeout|timed out|超时|服务暂不可用|暂时不可用/i;
const GENERIC_HTTP_ERROR_RE = /(?:请求失败|request failed)[^\n]*HTTP\s*\d{3}/i;
const INTERNAL_ERROR_DETAIL_RE =
    /\b(?:task_id|project_id|document_id|issue_id|material_id|aggregate_version|requested_version)\b|\b(?:B[1-4]|X[12])\b|WorkingFile|ReviewCycle|PatchSet|Revision Proposal|PendingDecision|\bFinding\b|stale aggregate version|服务端|后端|前端|接口约定|Traceback|SQL/i;
const CJK_RE = /[\u3400-\u9fff]/;

function publicApiErrorMessage(message: string, status: number): string {
    if (
        !message ||
        (CJK_RE.test(message) && !INTERNAL_ERROR_DETAIL_RE.test(message))
    )
        return message;
    if (status === 401) return "登录状态已失效，请重新登录。";
    if (status === 403) return "没有权限执行该操作。";
    if (status === 404) return "请求的内容不存在或不可用。";
    if (status === 409) return "当前内容已发生变化，请刷新后重试。";
    if (status >= 500) return "服务暂时不可用，请稍后重试。";
    return "操作未完成，请稍后重试。";
}

export function isVersionConflict(error: unknown): boolean {
    if (error instanceof ApiRequestError) {
        if (error.status === 409) return true;
        const code = (error.code ?? "").toLowerCase();
        if (code.includes("conflict") || code.includes("version")) return true;
    }
    const message = error instanceof Error ? error.message : String(error);
    return VERSION_CONFLICT_RE.test(message);
}

export function isRetryableError(error: unknown): boolean {
    if (error instanceof ApiRequestError) {
        if (error.retryable === true) return true;
        if (error.status === 0 || error.status >= 500) return true;
    }
    const message = error instanceof Error ? error.message : String(error);
    return RETRYABLE_ERROR_RE.test(message);
}

export function describeUserError(error: unknown, fallback = ""): string {
    if (isVersionConflict(error)) return "文档已被他人修改，请刷新后重试。";
    const status = error instanceof ApiRequestError ? error.status : 0;
    if (status === 401) return "登录状态已失效，请重新登录。";
    if (status === 403) return "没有权限执行该操作。";
    if (isRetryableError(error)) return "服务暂时不可用，请稍后重试。";
    const message = error instanceof Error ? error.message : String(error);
    if (GENERIC_HTTP_ERROR_RE.test(message))
        return fallback || "请求未完成，请稍后重试。";
    return publicApiErrorMessage(message, status) || fallback;
}

interface ErrorPayload {
    code?: string;
    message?: string;
    retryable?: boolean;
}

interface EnvelopeBody<T> {
    success?: boolean;
    data?: T;
    error?: ErrorPayload | null;
}

export async function requestRaw(
    path: string,
    init: RequestInit = {},
): Promise<Response> {
    try {
        return await fetch(path, { credentials: "include", ...init });
    } catch (reason) {
        throw new ApiRequestError(
            reason instanceof Error ? reason.message : String(reason),
            0,
            undefined,
            true,
        );
    }
}

function errorMessageFromBody(body: unknown, response: Response): string {
    if (body && typeof body === "object") {
        const record = body as {
            error?: { message?: string } | null;
            detail?: unknown;
        };
        if (record.error?.message) return record.error.message;
        if (typeof record.detail === "string" && record.detail) {
            return record.detail;
        }
        if (record.detail !== undefined && record.detail !== null) {
            try {
                return JSON.stringify(record.detail);
            } catch {
                return response.statusText || "Request failed";
            }
        }
    }
    if (typeof body === "string" && body) return body;
    return `${response.status} ${response.statusText}`;
}

export async function requestJson<T>(
    path: string,
    init: RequestInit = {},
): Promise<T> {
    const response = await requestRaw(path, init);
    const body = (await response.json().catch(() => null)) as
        | T
        | {
              error?: ErrorPayload | null;
              detail?: unknown;
              code?: string;
              retryable?: boolean;
          }
        | null;
    if (!response.ok) {
        const failure = body as {
            error?: ErrorPayload | null;
            code?: string;
            retryable?: boolean;
        } | null;
        const error = failure?.error;
        throw new ApiRequestError(
            errorMessageFromBody(body, response),
            response.status,
            error?.code ?? failure?.code ?? errorCodeForStatus(response.status),
            error?.retryable ?? failure?.retryable,
        );
    }
    return body as T;
}

export function jsonRequest(body: unknown, method = "POST"): RequestInit {
    return {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    };
}

export async function requestEnvelope<T>(
    path: string,
    init: RequestInit = {},
): Promise<T> {
    const response = await requestRaw(path, init);
    const body = (await response
        .json()
        .catch(() => null)) as EnvelopeBody<T> | null;
    if (response.ok && body?.success === true && body.data !== undefined) {
        return body.data;
    }
    throw new ApiRequestError(
        body?.error?.message ?? `请求失败（HTTP ${response.status}）`,
        response.status,
        body?.error?.code ?? errorCodeForStatus(response.status),
        body?.error?.retryable,
    );
}

export interface ScopedApiOptions {
    baseUrl?: string | (() => string);
    headers?: HeadersInit | (() => HeadersInit);
}

export function createScopedApi(prefix: string, options: ScopedApiOptions = {}) {
    const baseUrl = (): string => {
        const value =
            typeof options.baseUrl === "function"
                ? options.baseUrl()
                : (options.baseUrl ?? "");
        return value.replace(/\/$/, "");
    };
    const url = (suffix: string): string => `${baseUrl()}${prefix}${suffix}`;
    const defaults = (): HeadersInit =>
        typeof options.headers === "function"
            ? options.headers()
            : (options.headers ?? {});
    const withDefaults = (request: RequestInit = {}): RequestInit => {
        const headers = new Headers(defaults());
        new Headers(request.headers).forEach((value, key) =>
            headers.set(key, value),
        );
        return { ...request, headers };
    };
    return {
        url,
        requestJson: <T>(suffix: string, request?: RequestInit) =>
            requestJson<T>(url(suffix), withDefaults(request)),
        requestEnvelope: <T>(suffix: string, request?: RequestInit) =>
            requestEnvelope<T>(url(suffix), withDefaults(request)),
        withDefaults,
    };
}

function joinApiBase(apiBase: string, path: string): string {
    if (/^https?:\/\//i.test(path)) return path;
    const base = apiBase.replace(/\/$/, "");
    const suffix = path.startsWith("/") ? path : `/${path}`;
    return `${base}${suffix}`;
}

/**
 * Credentialed browser transport shared by Auth, Entry and Agent-domain adapters.
 * Consumers normally obtain this from `createPlatformClient()` instead of constructing it.
 */
export class BrowserHttpClient {
    constructor(readonly apiBase = "") {}

    url(path: string): string {
        return joinApiBase(this.apiBase, path);
    }

    request(path: string, init: RequestInit = {}): Promise<Response> {
        return requestRaw(this.url(path), init);
    }

    requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
        return requestJson<T>(this.url(path), init);
    }

    requestEnvelope<T>(path: string, init: RequestInit = {}): Promise<T> {
        return requestEnvelope<T>(this.url(path), init);
    }

    scoped(prefix: string, options: Omit<ScopedApiOptions, "baseUrl"> = {}) {
        return createScopedApi(prefix, {
            ...options,
            baseUrl: this.apiBase,
        });
    }

    axios(options: Omit<HttpOptions, "apiBase"> = {}): AxiosInstance {
        return createHttp({ ...options, apiBase: this.apiBase });
    }
}

const streamAdapter: AxiosAdapter = async (config) => {
    const body =
        config.data == null
            ? undefined
            : typeof config.data === "string"
              ? config.data
              : JSON.stringify(config.data);
    const init: RequestInit = {
        method: config.method ?? "GET",
        credentials: "include",
        headers: {
            ...(config.headers.toJSON() as Record<string, string>),
            "Content-Type": "application/json",
            Accept: "text/event-stream",
        },
        ...(body === undefined ? {} : { body }),
    };
    if (config.signal) init.signal = config.signal as AbortSignal;
    const response = await fetch(
        `${config.baseURL ?? ""}${config.url ?? ""}`,
        init,
    );
    if (!response.ok) {
        throw new axios.AxiosError(
            `Agent API ${response.status}: ${await response.text()}`,
            String(response.status),
            config,
        );
    }
    const headers: Record<string, string> = {};
    response.headers.forEach((value, key) => {
        headers[key] = value;
    });
    return {
        data: response,
        status: response.status,
        statusText: response.statusText,
        headers,
        config,
        request: response,
    };
};

export async function streamRequest(
    http: AxiosInstance,
    path: string,
    body: unknown,
    signal?: AbortSignal,
): Promise<Response> {
    const config: AxiosRequestConfig<typeof body> = {
        responseType: "stream",
        adapter: streamAdapter,
        headers: { Accept: "text/event-stream" },
    };
    if (signal) config.signal = signal;
    const response = await http.post<Response>(path, body, config);
    return response.data;
}
