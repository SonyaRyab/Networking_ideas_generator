import React, { useEffect, useRef, useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  FormControl,
  IconButton,
  MenuItem,
  Paper,
  Select,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import DarkModeOutlinedIcon from '@mui/icons-material/DarkModeOutlined';
import LightModeOutlinedIcon from '@mui/icons-material/LightModeOutlined';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import { useDispatch, useSelector } from 'react-redux';
import type { RootState } from '../store/store';
import {
  CONTENT_TYPE_LABELS,
  type ContentType,
  type GenerateRequest,
} from '../types/api';
import {
  addIdeaGroup,
  addTurn,
  completeTurn,
  failTurn,
  logout,
  setTurnStreaming,
  toggleTheme,
} from '../store/chatSlice';
import {
  abortAllConnections,
  extractAxiosErrorMessage,
  sendGenerateRequest,
  subscribeToEvents,
} from '../services/transportService';

function formatTime(isoString?: string): string {
  const date = isoString ? new Date(isoString) : new Date();
  return date.toLocaleTimeString('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function generateUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // версия 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // вариант RFC 4122
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export const ChatPage: React.FC = () => {
  const dispatch = useDispatch();
  const { username, darkMode, sessionId, turns } = useSelector(
    (state: RootState) => state.chat
  );

  const [topic, setTopic] = useState('');
  const [contentType, setContentType] = useState<ContentType | ''>('');
  const [requirements, setRequirements] = useState('');
  const [ideaCount, setIdeaCount] = useState<number>(12);
  const [topicError, setTopicError] = useState('');
  const [formatError, setFormatError] = useState('');

  const sessionRef = useRef(sessionId);
  const chatBottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    sessionRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns]);

  useEffect(() => {
    return () => {
      abortAllConnections();
    };
  }, []);

  const handleLogout = () => {
    abortAllConnections();
    setTopic('');
    setContentType('');
    setRequirements('');
    setIdeaCount(12);
    setTopicError('');
    setFormatError('');
    dispatch(logout());
  };

  const handleGenerate = async (e: React.FormEvent) => {
    e.preventDefault();

    const trimmedTopic = topic.trim();
    if (!trimmedTopic) {
      setTopicError('Введите тему для генерации');
      return;
    }
    if (trimmedTopic.length > 500) {
      setTopicError('Тема не должна превышать 500 символов');
      return;
    }

    const selectedType: ContentType = contentType || 'blog';
    if (!contentType) {
      setFormatError('Выберите формат контента');
      return;
    }

    const messageId = generateUUID();
    const sentAt = new Date().toISOString();
    const currentSession = sessionRef.current;

    const payload: GenerateRequest = {
      messageId,
      sender: username,
      sentAt,
      topic: trimmedTopic,
      contentType: selectedType,
      requirements: requirements.trim(),
      ideaCount,
    };

    dispatch(
      addTurn({
        messageId,
        sender: username,
        sentAt,
        topic: trimmedTopic,
        contentType: selectedType,
        requirements: requirements.trim(),
        ideaCount,
        status: 'pending',
        groups: [],
        receivedEventIds: [],
      })
    );

    try {
      const accepted = await sendGenerateRequest(payload);
      if (sessionRef.current !== currentSession) return;

      dispatch(setTurnStreaming({ messageId }));

      subscribeToEvents(messageId, accepted.eventsUrl, {
        onGroup: (event, eventId) => {
          if (sessionRef.current !== currentSession) return;
          dispatch(
            addIdeaGroup({
              messageId: event.messageId,
              eventId,
              group: event.group,
              respondedAt: new Date().toISOString(),
            })
          );
        },
        onCompleted: (event) => {
          if (sessionRef.current !== currentSession) return;
          dispatch(
            completeTurn({
              messageId: event.messageId,
              respondedAt: new Date().toISOString(),
            })
          );
        },
        onError: (event) => {
          if (sessionRef.current !== currentSession) return;
          const msg = event.error?.message
            ? `${event.error.code}: ${event.error.message}`
            : 'Ошибка генерации идей';
          dispatch(
            failTurn({
              messageId: event.messageId,
              errorMessage: msg,
              respondedAt: new Date().toISOString(),
            })
          );
        },
        onNetworkClosed: (msg) => {
          if (sessionRef.current !== currentSession) return;
          dispatch(
            failTurn({
              messageId,
              errorMessage: msg,
              respondedAt: new Date().toISOString(),
            })
          );
        },
      });
    } catch (err) {
      if (sessionRef.current !== currentSession) return;
      dispatch(
        failTurn({
          messageId,
          errorMessage: extractAxiosErrorMessage(err),
          respondedAt: new Date().toISOString(),
        })
      );
    }
  };

  // Цвета по макетам светлой и тёмной темы
  const colors = darkMode
    ? {
        headerBg: '#262628',
        headerText: '#FFFFFF',
        sidebarBg: '#3168A8',
        sidebarLabel: '#FFFFFF',
        chatHeaderBg: '#262628',
        chatBg: '#19191C',
        bubbleBg: '#2C2D30',
        bubbleText: '#E1E3E6',
        timeText: '#8E9299',
        border: '#38383C',
        themeBtnBg: '#1C1C1E',
        themeBtnIcon: '#8E9299',
      }
    : {
        headerBg: '#FFFFFF',
        headerText: '#000000',
        sidebarBg: '#E3EFFC',
        sidebarLabel: '#000000',
        chatHeaderBg: '#FFFFFF',
        chatBg: '#EBEDF0',
        bubbleBg: '#FFFFFF',
        bubbleText: '#000000',
        timeText: '#818C99',
        border: '#DCE1E6',
        themeBtnBg: '#0077FF',
        themeBtnIcon: '#000000',
      };

  const inputSx = {
    '& .MuiOutlinedInput-root': {
      borderRadius: '18px',
      bgcolor: '#FFFFFF',
      color: '#000000',
      '& fieldset': { border: 'none' },
    },
    '& .MuiInputBase-input::placeholder': {
      color: '#4A76A8',
      opacity: 0.85,
    },
  };

  return (
    <Box
      sx={{
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        bgcolor: colors.chatBg,
      }}
    >
      {/* Верхняя шапка */}
      <Box
        sx={{
          height: 68,
          px: 3,
          bgcolor: colors.headerBg,
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexShrink: 0,
        }}
      >
        <Typography
          variant="h6"
          sx={{ fontWeight: 700, color: colors.headerText }}
        >
          Пользователь: {username.toUpperCase()}
        </Typography>

        <Button
          variant="contained"
          disableElevation
          onClick={handleLogout}
          sx={{
            bgcolor: '#0077FF',
            color: '#FFFFFF',
            borderRadius: '20px',
            px: 3,
            textTransform: 'none',
            fontWeight: 600,
            '&:hover': { bgcolor: '#0066DD' },
          }}
        >
          Выйти
        </Button>
      </Box>

      {/* Основная область: левая панель + чат */}
      <Box sx={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {/* Левая панель формы */}
        <Box
          component="form"
          onSubmit={handleGenerate}
          sx={{
            width: 340,
            flexShrink: 0,
            bgcolor: colors.sidebarBg,
            p: 3,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
            overflowY: 'auto',
          }}
        >
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2.5 }}>
            {/* Тема */}
            <Box>
              <Typography
                sx={{ fontWeight: 700, mb: 1, color: colors.sidebarLabel }}
              >
                Тема
              </Typography>
              <TextField
                fullWidth
                size="small"
                placeholder="Введите тему"
                value={topic}
                onChange={(e) => {
                  setTopic(e.target.value);
                  if (topicError) setTopicError('');
                }}
                error={Boolean(topicError)}
                helperText={topicError}
                slotProps={{
                  formHelperText: {
                    sx: {
                      color: darkMode ? '#FFD166' : '#D32F2F',
                      fontWeight: 500,
                      mx: 1,
                    },
                  },
                }}
                sx={inputSx}
              />
            </Box>

            {/* Формат контента */}
            <Box>
              <Typography
                sx={{ fontWeight: 700, mb: 1, color: colors.sidebarLabel }}
              >
                Формат контента
              </Typography>
              <FormControl fullWidth size="small" error={Boolean(formatError)}>
                <Select
                  displayEmpty
                  value={contentType}
                  IconComponent={KeyboardArrowDownIcon}
                  onChange={(e) => {
                    setContentType(e.target.value as ContentType);
                    if (formatError) setFormatError('');
                  }}
                  sx={{
                    borderRadius: '18px',
                    bgcolor: '#FFFFFF',
                    color: contentType ? '#000000' : '#4A76A8',
                    '& .MuiOutlinedInput-notchedOutline': { border: 'none' },
                    '& .MuiSelect-icon': { color: '#3168A8' },
                  }}
                >
                  <MenuItem value="" disabled>
                    Выберите из списка
                  </MenuItem>
                  {(Object.keys(CONTENT_TYPE_LABELS) as ContentType[]).map(
                    (key) => (
                      <MenuItem key={key} value={key}>
                        {CONTENT_TYPE_LABELS[key]}
                      </MenuItem>
                    )
                  )}
                </Select>
                {formatError && (
                  <Typography
                    variant="caption"
                    sx={{
                      color: darkMode ? '#FFD166' : '#D32F2F',
                      mt: 0.5,
                      mx: 1,
                    }}
                  >
                    {formatError}
                  </Typography>
                )}
              </FormControl>
            </Box>

            {/* Дополнительные пожелания */}
            <Box>
              <Typography
                sx={{ fontWeight: 700, mb: 1, color: colors.sidebarLabel }}
              >
                Дополнительные пожелания
              </Typography>
              <TextField
                fullWidth
                size="small"
                multiline
                minRows={1}
                maxRows={4}
                placeholder="Введите пожелания, уточнения"
                value={requirements}
                onChange={(e) => setRequirements(e.target.value)}
                slotProps={{
                  htmlInput: {
                    maxLength: 2000,
                  },
                }}
                sx={inputSx}
              />
            </Box>

            {/* Количество идей (10-15) */}
            <Box>
              <Typography
                sx={{ fontWeight: 700, mb: 1, color: colors.sidebarLabel }}
              >
                Количество идей
              </Typography>
              <Select
                fullWidth
                size="small"
                value={ideaCount}
                IconComponent={KeyboardArrowDownIcon}
                onChange={(e) => setIdeaCount(Number(e.target.value))}
                sx={{
                  borderRadius: '18px',
                  bgcolor: '#FFFFFF',
                  color: '#000000',
                  '& .MuiOutlinedInput-notchedOutline': { border: 'none' },
                  '& .MuiSelect-icon': { color: '#3168A8' },
                }}
              >
                {[10, 11, 12, 13, 14, 15].map((count) => (
                  <MenuItem key={count} value={count}>
                    {count} идей
                  </MenuItem>
                ))}
              </Select>
            </Box>

            <Button
              type="submit"
              variant="contained"
              disableElevation
              sx={{
                alignSelf: 'flex-start',
                bgcolor: '#0077FF',
                color: '#FFFFFF',
                borderRadius: '20px',
                px: 3.5,
                py: 1,
                textTransform: 'none',
                fontWeight: 600,
                fontSize: '0.95rem',
                '&:hover': { bgcolor: '#0066DD' },
              }}
            >
              Сгенерировать
            </Button>
          </Box>

          {/* Переключатель темы */}
          <Box sx={{ mt: 3 }}>
            <IconButton
              onClick={() => dispatch(toggleTheme())}
              aria-label="Переключить тему"
              sx={{
                width: 44,
                height: 44,
                bgcolor: colors.themeBtnBg,
                color: colors.themeBtnIcon,
                '&:hover': {
                  bgcolor: darkMode ? '#2C2C2E' : '#0066DD',
                },
              }}
            >
              {darkMode ? <LightModeOutlinedIcon /> : <DarkModeOutlinedIcon />}
            </IconButton>
          </Box>
        </Box>

        {/* Правая часть: окно чата */}
        <Box
          sx={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            minWidth: 0,
          }}
        >
          <Box
            sx={{
              px: 3,
              py: 1.5,
              bgcolor: colors.chatHeaderBg,
              borderBottom: `1px solid ${colors.border}`,
            }}
          >
            <Typography
              variant="subtitle1"
              sx={{ fontWeight: 700, color: colors.headerText }}
            >
              Чат
            </Typography>
          </Box>

          <Box
            sx={{
              flex: 1,
              overflowY: 'auto',
              p: 3,
              display: 'flex',
              flexDirection: 'column',
              gap: 2.5,
            }}
          >
            {turns.map((turn) => (
              <React.Fragment key={turn.messageId}>
                {/* Сообщение пользователя (справа) */}
                <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <Paper
                    elevation={0}
                    sx={{
                      maxWidth: 340,
                      p: 2,
                      borderRadius: '16px',
                      bgcolor: colors.bubbleBg,
                      color: colors.bubbleText,
                    }}
                  >
                    <Typography sx={{ fontWeight: 700, mb: 0.75 }}>
                      {turn.sender}
                    </Typography>
                    <Typography variant="body2" sx={{ mb: 0.5 }}>
                      Тема: {turn.topic}
                    </Typography>
                    <Typography variant="body2" sx={{ mb: 0.5 }}>
                      Формат: {CONTENT_TYPE_LABELS[turn.contentType]}
                    </Typography>
                    {turn.requirements && (
                      <Typography variant="body2" sx={{ mb: 0.5 }}>
                        Пожелания: {turn.requirements}
                      </Typography>
                    )}
                    <Typography
                      variant="caption"
                      sx={{
                        display: 'block',
                        textAlign: 'right',
                        color: colors.timeText,
                        mt: 0.75,
                      }}
                    >
                      {formatTime(turn.sentAt)}
                    </Typography>
                  </Paper>
                </Box>

                {/* Ответ агента (слева) */}
                <Box sx={{ display: 'flex', justifyContent: 'flex-start' }}>
                  <Paper
                    elevation={0}
                    sx={{
                      maxWidth: turn.status === 'error' ? 220 : 560,
                      minWidth: 140,
                      p: 2,
                      borderRadius: '16px',
                      bgcolor: colors.bubbleBg,
                      color: colors.bubbleText,
                    }}
                  >
                    <Typography sx={{ fontWeight: 700, mb: 1 }}>Агент</Typography>

                    {/* Ожидание ответа */}
                    {(turn.status === 'pending' ||
                      (turn.status === 'streaming' && turn.groups.length === 0)) && (
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, py: 0.5 }}>
                        <CircularProgress size={18} />
                        <Typography variant="body2">Генерация идей...</Typography>
                      </Box>
                    )}

                    {/* Группы идей */}
                    {turn.groups.length > 0 && (
                      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                        {turn.groups.map((group) => (
                          <Box key={group.groupNumber}>
                            <Typography
                              variant="body2"
                              sx={{ fontWeight: 600, mb: 0.5 }}
                            >
                              {group.title || `Группа ${group.groupNumber}`}
                            </Typography>
                            {group.ideas.map((idea) => (
                              <Typography
                                key={idea.number}
                                variant="body2"
                                sx={{ mb: 0.75, lineHeight: 1.4 }}
                              >
                                {idea.number}. {idea.title}: {idea.description}
                              </Typography>
                            ))}
                          </Box>
                        ))}
                      </Box>
                    )}

                    {/* Ошибка генерации / доставки */}
                    {turn.status === 'error' && (
                      <Tooltip title={turn.errorMessage || 'Ошибка генерации'}>
                        <Box
                          sx={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 1.5,
                            my: 0.5,
                          }}
                        >
                          <Box
                            sx={{
                              width: 44,
                              height: 44,
                              borderRadius: '50%',
                              bgcolor: '#FFEE00',
                              color: '#000000',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              fontWeight: 800,
                              fontSize: '1.6rem',
                              flexShrink: 0,
                            }}
                          >
                            !
                          </Box>
                        </Box>
                      </Tooltip>
                    )}

                    <Typography
                      variant="caption"
                      sx={{
                        display: 'block',
                        textAlign: 'right',
                        color: colors.timeText,
                        mt: 1,
                      }}
                    >
                      {formatTime(turn.agentRespondedAt || turn.sentAt)}
                    </Typography>
                  </Paper>
                </Box>
              </React.Fragment>
            ))}
            <div ref={chatBottomRef} />
          </Box>
        </Box>
      </Box>
    </Box>
  );
};