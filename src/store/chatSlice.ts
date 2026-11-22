import { createSlice, type PayloadAction } from '@reduxjs/toolkit';
import type { ChatTurn, IdeaGroup } from '../types/api';

interface ChatState {
  username: string;
  isAuthenticated: boolean;
  darkMode: boolean;
  sessionId: number;
  turns: ChatTurn[];
}

const initialState: ChatState = {
  username: '',
  isAuthenticated: false,
  darkMode: false,
  sessionId: 0,
  turns: [],
};

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    login(state, action: PayloadAction<string>) {
      state.username = action.payload.trim();
      state.isAuthenticated = true;
      state.sessionId += 1;
      state.turns = [];
    },
    logout(state) {
      state.username = '';
      state.isAuthenticated = false;
      state.sessionId += 1;
      state.turns = [];
    },
    toggleTheme(state) {
      state.darkMode = !state.darkMode;
    },
    addTurn(state, action: PayloadAction<ChatTurn>) {
      state.turns.push(action.payload);
    },
    setTurnStreaming(state, action: PayloadAction<{ messageId: string }>) {
      const turn = state.turns.find((t) => t.messageId === action.payload.messageId);
      if (turn && turn.status === 'pending') {
        turn.status = 'streaming';
      }
    },
    addIdeaGroup(
      state,
      action: PayloadAction<{
        messageId: string;
        eventId?: string;
        group: IdeaGroup;
        respondedAt: string;
      }>
    ) {
      const turn = state.turns.find((t) => t.messageId === action.payload.messageId);
      if (!turn || turn.status === 'error') return;

      // Дедупликация по id события и номеру группы
      if (action.payload.eventId && turn.receivedEventIds.includes(action.payload.eventId)) {
        return;
      }
      const exists = turn.groups.some(
        (g) => g.groupNumber === action.payload.group.groupNumber
      );
      if (!exists) {
        turn.groups.push(action.payload.group);
        turn.groups.sort((a, b) => a.groupNumber - b.groupNumber);
      }
      if (action.payload.eventId) {
        turn.receivedEventIds.push(action.payload.eventId);
      }
      turn.status = 'streaming';
      turn.agentRespondedAt = action.payload.respondedAt;
    },
    completeTurn(
      state,
      action: PayloadAction<{ messageId: string; respondedAt: string }>
    ) {
      const turn = state.turns.find((t) => t.messageId === action.payload.messageId);
      if (!turn || turn.status === 'error') return;
      turn.status = 'completed';
      turn.agentRespondedAt = action.payload.respondedAt;
    },
    failTurn(
      state,
      action: PayloadAction<{
        messageId: string;
        errorMessage: string;
        respondedAt: string;
      }>
    ) {
      const turn = state.turns.find((t) => t.messageId === action.payload.messageId);
      if (!turn) return;
      turn.status = 'error';
      turn.groups = []; // При ошибке частичный результат не показывается
      turn.errorMessage = action.payload.errorMessage;
      turn.agentRespondedAt = action.payload.respondedAt;
    },
  },
});

export const {
  login,
  logout,
  toggleTheme,
  addTurn,
  setTurnStreaming,
  addIdeaGroup,
  completeTurn,
  failTurn,
} = chatSlice.actions;

export default chatSlice.reducer;