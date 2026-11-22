import React from 'react';
import { CssBaseline, ThemeProvider, createTheme } from '@mui/material';
import { useSelector } from 'react-redux';
import type { RootState } from './store/store';
import { LoginPage } from './components/LoginPage';
import { ChatPage } from './components/ChatPage';

export const App: React.FC = () => {
  const { isAuthenticated, darkMode } = useSelector(
    (state: RootState) => state.chat
  );

  const theme = createTheme({
    palette: {
      mode: darkMode ? 'dark' : 'light',
      primary: { main: '#0077FF' },
    },
    typography: {
      fontFamily: '"Inter", "Roboto", "Helvetica", "Arial", sans-serif',
    },
  });

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {isAuthenticated ? <ChatPage /> : <LoginPage />}
    </ThemeProvider>
  );
};

export default App;