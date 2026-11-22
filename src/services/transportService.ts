import axios, { type AxiosError } from 'axios';
import type {
  AcceptedResponse,
  ApiErrorBody,
  GenerateRequest,
  SseCompletedEvent,
  SseErrorEvent,
  SseGroupEvent,
} from '../types/api';

const TRANSPORT_BASE_URL = (
  import.meta.env.VITE_TRANSPORT_URL || 'http://localhost:8080'
).replace(/\/+$/, '');

const apiClient = axios.create({
  baseURL: TRANSPORT_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

const activeEventSources = new Map<string, EventSource>();
const activeControllers = new Map<string, AbortController>();

function resolveUrl(url: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  return `${TRANSPORT_BASE_URL}${url.startsWith('/') ? '' : '/'}${url}`;
}

export async function sendGenerateRequest(
  payload: GenerateRequest
): Promise<AcceptedResponse> {
  const controller = new AbortController();
  activeControllers.set(payload.messageId, controller);

  try {
    const response = await apiClient.post<AcceptedResponse>(
      '/api/v1/requests',
      payload,
      { signal: controller.signal }
    );
    return response.data;
  } finally {
    activeControllers.delete(payload.messageId);
  }
}

export function extractAxiosErrorMessage(err: unknown): string {
  if (axios.isCancel(err)) {
    return 'Запрос отменён';
  }
  const axiosErr = err as AxiosError<ApiErrorBody>;
  if (axiosErr.response?.data?.message) {
    return `${axiosErr.response.data.code}: ${axiosErr.response.data.message}`;
  }
  if (axiosErr.response) {
    return `Ошибка сервера (HTTP ${axiosErr.response.status})`;
  }
  return 'Не удалось подключиться к транспортному сервису';
}

export interface StreamHandlers {
  onGroup: (event: SseGroupEvent, eventId?: string) => void;
  onCompleted: (event: SseCompletedEvent) => void;
  onError: (event: SseErrorEvent) => void;
  onNetworkClosed: (message: string) => void;
}

export function subscribeToEvents(
  messageId: string,
  eventsUrl: string,
  handlers: StreamHandlers
): void {
  closeEventSource(messageId);

  const es = new EventSource(resolveUrl(eventsUrl));
  activeEventSources.set(messageId, es);

  es.addEventListener('idea_group', (e: MessageEvent) => {
    try {
      const data: SseGroupEvent = JSON.parse(e.data);
      handlers.onGroup(data, e.lastEventId || undefined);
    } catch {
      // Игнорируем некорректный JSON
    }
  });

  es.addEventListener('generation_completed', (e: MessageEvent) => {
    try {
      const data: SseCompletedEvent = JSON.parse(e.data);
      handlers.onCompleted(data);
    } finally {
      closeEventSource(messageId);
    }
  });

  es.addEventListener('generation_error', (e: MessageEvent) => {
    try {
      const data: SseErrorEvent = JSON.parse(e.data);
      handlers.onError(data);
    } finally {
      closeEventSource(messageId);
    }
  });

  es.onerror = () => {
    // EventSource автоматически переподключается с Last-Event-ID.
    // Если соединение закрыто окончательно, уведомляем интерфейс.
    if (es.readyState === EventSource.CLOSED) {
      closeEventSource(messageId);
      handlers.onNetworkClosed('Соединение с сервером прервано');
    }
  };
}

export function closeEventSource(messageId: string): void {
  const es = activeEventSources.get(messageId);
  if (es) {
    es.close();
    activeEventSources.delete(messageId);
  }
}

export function abortAllConnections(): void {
  activeControllers.forEach((controller) => controller.abort());
  activeControllers.clear();

  activeEventSources.forEach((es) => es.close());
  activeEventSources.clear();
}