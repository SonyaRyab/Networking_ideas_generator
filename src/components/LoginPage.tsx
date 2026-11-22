import React, { useState } from 'react';
import { Box, Button, Paper, TextField, Typography } from '@mui/material';
import { useDispatch } from 'react-redux';
import { login } from '../store/chatSlice';

export const LoginPage: React.FC = () => {
  const dispatch = useDispatch();
  const [name, setName] = useState('');
  const [error, setError] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError('Введите имя для входа');
      return;
    }
    if (trimmed.length > 50) {
      setError('Имя не должно превышать 50 символов');
      return;
    }
    dispatch(login(trimmed));
  };

  return (
    <Box
      sx={{
        minHeight: '100vh',
        bgcolor: '#EBEDF0',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        p: 2,
      }}
    >
      <Paper
        elevation={0}
        component="form"
        onSubmit={handleSubmit}
        sx={{
          width: '100%',
          maxWidth: 420,
          p: 4,
          borderRadius: '24px',
          bgcolor: '#FFFFFF',
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
        }}
      >
        <Typography
          variant="h4"
          align="center"
          sx={{ color: '#0077FF', fontWeight: 700, mb: 1 }}
        >
          Вход
        </Typography>

        <Box>
          <Typography variant="body1" sx={{ mb: 1, color: '#000000' }}>
            Введите имя
          </Typography>
          <TextField
            fullWidth
            placeholder="Ваше имя"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (error) setError('');
            }}
            error={Boolean(error)}
            helperText={error}
            variant="outlined"
            sx={{
              '& .MuiOutlinedInput-root': {
                borderRadius: '20px',
                bgcolor: '#EBEDF0',
                '& fieldset': { border: 'none' },
              },
            }}
          />
        </Box>

        <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 1 }}>
          <Button
            type="submit"
            variant="contained"
            disableElevation
            sx={{
              bgcolor: '#0077FF',
              color: '#FFFFFF',
              borderRadius: '20px',
              px: 4,
              py: 1,
              textTransform: 'none',
              fontWeight: 600,
              fontSize: '1rem',
              '&:hover': { bgcolor: '#0066DD' },
            }}
          >
            Войти
          </Button>
        </Box>
      </Paper>
    </Box>
  );
};