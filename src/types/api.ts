export type ContentType = 'blog' | 'video' | 'post' | 'post_series' | 'other';

export const CONTENT_TYPE_LABELS: Record<ContentType, string> = {
  blog: 'Блог',
  video: 'Видео',
  post: 'Пост в соцсетях',
  post_series: 'Серия постов',
  other: 'Другое',
};

export interface GenerateRequest {
  messageId: string;
  sender: string;
  sentAt: string; // RFC3339 / ISO 8601
  topic: string;
  contentType: ContentType;
  requirements?: string;
  ideaCount?: number; // 10..15, по умолчанию 12
}

export interface AcceptedResponse {
  messageId: string;
  statusUrl: string;
  eventsUrl: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: unknown;
}

export interface IdeaItem {
  number: number;
  title: string;
  description: string;
}

export interface IdeaGroup {
  groupNumber: number;
  title?: string;
  ideas: IdeaItem[];
}

export interface SseGroupEvent {
  type: 'idea_group';
  messageId: string;
  sentAt: string;
  isError: false;
  totalGroups: number;
  group: IdeaGroup;
}

export interface SseCompletedEvent {
  type: 'generation_completed';
  messageId: string;
  sentAt: string;
  isError: false;
  ideaCount: number;
}

export interface SseErrorEvent {
  type: 'generation_error';
  messageId: string;
  sentAt: string;
  isError: true;
  error: ApiErrorBody;
}

export type MessageStatus = 'pending' | 'streaming' | 'completed' | 'error';

export interface ChatTurn {
  messageId: string;
  sender: string;
  sentAt: string;
  topic: string;
  contentType: ContentType;
  requirements: string;
  ideaCount: number;
  status: MessageStatus;
  groups: IdeaGroup[];
  receivedEventIds: string[];
  agentRespondedAt?: string;
  errorMessage?: string;
}