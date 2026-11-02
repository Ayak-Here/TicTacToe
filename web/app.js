const boardElement = document.querySelector('#board');
const guestKey = 'three-row-guest-record';
const guestRecord = () => ({ wins: 0, losses: 0, played: 0, streak: 0 });

function readGuestRecord() {
  try {
    const record = JSON.parse(localStorage.getItem(guestKey));
    return record && ['wins', 'played', 'streak'].every(key => Number.isSafeInteger(record[key]) && record[key] >= 0)
      ? { ...guestRecord(), ...record } : guestRecord();
  } catch { return guestRecord(); }
}

const state = {
  screen: 'home', authPanel: 'login', game: null, mode: 'cpu', variant: 'classic',
  account: null, history: [], guest: readGuestRecord(), round: 1, busy: false, toastTimer: null,
};

function showScreen(screen, authPanel = state.authPanel, updateUrl = true) {
  state.screen = screen;
  state.authPanel = authPanel;
  document.querySelectorAll('[data-screen]').forEach(element => element.classList.toggle('hidden', element.dataset.screen !== screen));
  document.querySelectorAll('[data-auth-panel]').forEach(element => element.classList.toggle('hidden', element.dataset.authPanel !== authPanel));
  if (updateUrl) history.replaceState(null, '', `#${screen === 'auth' ? authPanel : screen}`);
  document.querySelector('#form-error').textContent = '';
}

function showError(message) {
  const element = state.screen === 'auth' ? document.querySelector('#form-error') : document.querySelector('#toast');
  element.replaceChildren(document.createTextNode(message));
  if (element.id === 'toast') {
    element.classList.add('visible');
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => element.classList.remove('visible'), 2600);
  }
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || 'Something went wrong. Please try again.');
    error.data = data;
    throw error;
  }
  return data;
}

function formData(form) { return Object.fromEntries(new FormData(form).entries()); }

async function refreshAccount() {
  const data = await api('/api/me');
  state.account = data.user;
  state.history = data.history || [];
  const signedIn = Boolean(state.account);
  document.querySelector('#main-nav').classList.toggle('hidden', signedIn);
  document.querySelector('#account-nav').classList.toggle('hidden', !signedIn);
  document.querySelector('#nav-email').textContent = state.account?.email || '';
  document.querySelector('#progress-card').classList.toggle('hidden', !signedIn);
  document.querySelector('#guest-progress').classList.toggle('hidden', signedIn);
  renderProgress();
}

function renderProgress() {
  if (state.account) {
    const total = state.account.wins + state.account.losses + state.account.draws;
    document.querySelector('#account-wins').textContent = state.account.wins;
    document.querySelector('#account-played').textContent = total;
    document.querySelector('#account-streak').textContent = state.account.streak;
    document.querySelector('#match-score-x').textContent = state.account.wins;
    document.querySelector('#match-score-o').textContent = state.account.losses;
    const rank = total === 0 ? 'NEWCOMER' : state.account.wins >= 25 ? 'GRANDMASTER' : state.account.wins >= 10 ? 'CONTENDER' : state.account.wins >= 3 ? 'REGULAR' : 'WARMING UP';
    document.querySelector('#rank-label').textContent = rank;
    const historyList = document.querySelector('#history-list');
    historyList.replaceChildren();
    state.history.slice(0, 4).forEach(match => {
      const row = document.createElement('div');
      row.className = 'history-item';
      const variant = match.variant === 'four' ? 'Four in a Row' : match.variant === 'misere' ? 'Misère' : 'Classic';
      const result = match.result === 'win' ? 'WIN' : match.result === 'draw' ? 'DRAW' : match.result === 'loss' ? 'LOSS' : `${match.result.toUpperCase()} WINS`;
      const left = document.createElement('span'); left.textContent = variant;
      const right = document.createElement('b'); right.textContent = result;
      row.append(left, right); historyList.append(row);
    });
  } else {
    document.querySelector('#guest-wins').textContent = state.guest.wins;
    document.querySelector('#guest-played').textContent = state.guest.played;
    document.querySelector('#guest-streak').textContent = state.guest.streak;
    document.querySelector('#match-score-x').textContent = state.guest.wins;
    document.querySelector('#match-score-o').textContent = state.guest.losses;
  }
}

function lineType(line, size) {
  if (!line.length) return '';
  const firstRow = Math.floor(line[0] / size);
  const firstCol = line[0] % size;
  if (line.every(index => Math.floor(index / size) === firstRow)) return `row-${firstRow}`;
  if (line.every(index => index % size === firstCol)) return `col-${firstCol}`;
  return line[0] === 0 ? 'diag-down' : 'diag-up';
}

function renderBoard() {
  const game = state.game;
  boardElement.replaceChildren();
  boardElement.dataset.size = game.size;
  boardElement.setAttribute('aria-label', game.variant === 'four' ? 'Four in a Row board' : 'Tic Tac Toe board');

  game.board.forEach((mark, index) => {
    const cell = document.createElement('button');
    cell.className = `cell${mark ? ` ${mark.toLowerCase()}` : ''}`;
    cell.type = 'button';
    cell.setAttribute('role', 'gridcell');
    cell.setAttribute('aria-label', mark ? `Cell ${index + 1}, ${mark}` : `Play cell ${index + 1}`);
    cell.disabled = Boolean(mark) || game.status !== 'playing' || state.busy
      || (game.mode === 'cpu' && game.turn === game.aiMark);
    if (mark) {
      const glyph = document.createElement('span');
      glyph.className = 'cell-mark';
      glyph.textContent = mark;
      cell.append(glyph);
    }
    if (game.winningLine.includes(index)) cell.classList.add('winner');
    if (game.status === 'finished' && game.winner === 'draw') cell.classList.add('drawn');
    cell.addEventListener('click', () => play(index));
    boardElement.append(cell);
  });
}

function renderStatus() {
  const game = state.game;
  const symbol = document.querySelector('#status-symbol');
  const kicker = document.querySelector('#status-kicker');
  const message = document.querySelector('#status-message');
  const sweep = document.querySelector('#winning-sweep');
  sweep.className = 'winning-sweep';
  document.querySelector('#pip-x').classList.toggle('active', game.status === 'playing' && game.turn === 'X');
  document.querySelector('#pip-o').classList.toggle('active', game.status === 'playing' && game.turn === 'O');

  if (game.status === 'finished') {
    if (game.winner === 'draw') {
      symbol.textContent = '↔'; symbol.className = 'status-mark draw';
      kicker.textContent = 'A DRAW'; message.textContent = 'Well matched. Another?';
    } else {
      symbol.textContent = game.winner; symbol.className = `status-mark ${game.winner.toLowerCase()}`;
      const youWon = game.mode === 'cpu' && game.winner === 'X';
      kicker.textContent = youWon ? 'YOU WIN' : game.mode === 'cpu' ? 'COMPUTER WINS' : `${game.winner} WINS`;
      message.textContent = youWon ? 'Nicely played.' : game.mode === 'cpu' ? 'Close one. Try again?' : 'Nicely played.';
      const lineMark = game.variant === 'misere' ? game.board[game.winningLine[0]] : game.winner;
      sweep.className = `winning-sweep ${lineType(game.winningLine, game.size)} ${lineMark.toLowerCase()}`;
      if (game.size === 4) sweep.classList.add('four');
    }
    return;
  }

  symbol.textContent = game.turn;
  symbol.className = `status-mark ${game.turn.toLowerCase()}`;
  if (state.busy && game.mode === 'local') {
    kicker.textContent = 'UPDATING BOARD';
    message.textContent = 'Your move is appearing…';
    return;
  }
  const yourTurn = game.mode === 'local' || game.turn === 'X';
  kicker.textContent = game.mode === 'local' ? `${game.turn} TO PLAY` : yourTurn ? 'YOUR TURN' : 'COMPUTER THINKING';
  message.textContent = game.mode === 'local' ? 'Make your move.' : yourTurn ? 'Make it count.' : 'Choosing a move…';
}

function renderGame() {
  if (!state.game) return;
  renderBoard();
  renderStatus();
  document.querySelector('#round-label').textContent = `ROUND ${String(state.round).padStart(2, '0')}`;
  const variantTitle = state.variant === 'four' ? 'FOUR IN A ROW' : state.variant === 'misere' ? 'MISÈRE' : 'CLASSIC';
  document.querySelector('#mode-caption').textContent = `${state.mode === 'cpu' ? 'SOLO' : 'FRIEND'} · ${variantTitle}`;
  document.querySelector('#name-fields').classList.toggle('hidden', state.mode !== 'local');
  document.querySelector('#player-x-label').textContent = state.mode === 'local' ? state.game.playerX : 'YOU';
  document.querySelector('#player-o-label').textContent = state.mode === 'local' ? state.game.playerO : 'COMPUTER';
  document.querySelector('#player-x-card').classList.toggle('turn-active', state.game.status === 'playing' && state.game.turn === 'X');
  document.querySelector('#player-o-card').classList.toggle('turn-active', state.game.status === 'playing' && state.game.turn === 'O');
}

async function startGame() {
  state.busy = true;
  showScreen('game');
  try {
    state.game = await api('/api/games', {
      method: 'POST',
      body: JSON.stringify({ mode: state.mode, variant: state.variant, playerX: document.querySelector('#player-x-name').value, playerO: document.querySelector('#player-o-name').value }),
    });
    state.round = 1;
    state.busy = false;
    renderGame();
  } catch (error) {
    state.busy = false;
    showError(error.message);
  }
}

async function newGame() {
  if (state.busy) return;
  state.busy = true;
  try {
    state.game = await api('/api/games', {
      method: 'POST',
      body: JSON.stringify({ mode: state.mode, variant: state.variant, playerX: document.querySelector('#player-x-name').value, playerO: document.querySelector('#player-o-name').value }),
    });
    state.round += 1;
    state.busy = false;
    renderGame();
  } catch (error) {
    state.busy = false;
    showError(error.message);
  }
}

async function play(cell) {
  if (state.busy || !state.game || state.game.status !== 'playing') return;
  const beforeMove = state.game;
  const optimisticGame = { ...beforeMove, board: [...beforeMove.board], winningLine: [...beforeMove.winningLine] };
  optimisticGame.board[cell] = optimisticGame.turn;
  optimisticGame.turn = beforeMove.mode === 'cpu'
    ? beforeMove.aiMark
    : beforeMove.turn === 'X' ? 'O' : 'X';
  state.game = optimisticGame;
  state.busy = true;
  renderGame();
  try {
    state.game = await api(`/api/games/${beforeMove.id}/move`, {
      method: 'POST', body: JSON.stringify({ cell }),
    });
    state.busy = false;
    renderGame();
    if (state.game.status === 'finished') await recordResult();
  } catch (error) {
    state.game = beforeMove;
    state.busy = false;
    renderGame();
    showError(error.message);
  }
}

async function recordResult() {
  if (state.account) {
    try { await refreshAccount(); } catch (error) { showError(error.message); }
  } else {
    state.guest.played += 1;
    if (state.game.winner === 'X') { state.guest.wins += 1; state.guest.streak += 1; }
    else if (state.game.winner === 'O') { state.guest.losses += 1; state.guest.streak = 0; }
    try { localStorage.setItem(guestKey, JSON.stringify(state.guest)); } catch { /* Guest stats stay available until the page closes. */ }
    renderProgress();
  }
}

function setupForm(id, handler) {
  document.querySelector(id).addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    document.querySelector('#form-error').replaceChildren();
    try { await handler(formData(form), form); }
    catch (error) {
      showError(error.message);
      if (error.data?.accountExists) {
        const signIn = document.createElement('button');
        signIn.type = 'button';
        signIn.className = 'inline-link';
        signIn.textContent = 'Sign in instead';
        signIn.addEventListener('click', () => {
          document.querySelector('#login-form').elements.email.value = error.data.email || form.elements.email.value;
          showScreen('auth', 'login');
        });
        document.querySelector('#form-error').append(' ', signIn);
      }
    } finally { submit.disabled = false; }
  });
}

setupForm('#register-form', async data => {
  if (data.password !== data.confirmPassword) throw new Error('The passwords do not match.');
  await api('/api/auth/register', { method: 'POST', body: JSON.stringify(data) });
  await refreshAccount();
  showScreen('game');
  await startGame();
});

setupForm('#login-form', async data => {
  await api('/api/auth/login', { method: 'POST', body: JSON.stringify(data) });
  await refreshAccount();
  showScreen('game');
  await startGame();
});

document.querySelectorAll('[data-auth-link]').forEach(button => button.addEventListener('click', () => showScreen('auth', button.dataset.authLink)));
document.querySelector('.wordmark').addEventListener('click', event => {
  event.preventDefault();
  showScreen('home');
});
const helpDialog = document.querySelector('#how-to-play-dialog');
document.querySelectorAll('[data-help-open]').forEach(button => button.addEventListener('click', () => {
  if (!helpDialog.open) helpDialog.showModal();
}));
document.querySelector('[data-help-close]').addEventListener('click', () => helpDialog.close());
helpDialog.addEventListener('click', event => {
  if (event.target === helpDialog) helpDialog.close();
});
document.querySelectorAll('[data-screen-link]').forEach(button => button.addEventListener('click', () => {
  if (button.dataset.screenLink === 'game' && !state.game) startGame();
  else showScreen(button.dataset.screenLink);
}));
document.querySelector('#guest-start').addEventListener('click', startGame);
document.querySelector('#new-game').addEventListener('click', newGame);
document.querySelector('#logout-button').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST', body: '{}' }); await refreshAccount(); showScreen('home'); }
  catch (error) { showError(error.message); }
});

document.querySelectorAll('.variant-card').forEach(button => button.addEventListener('click', () => {
  state.variant = button.dataset.variant;
  document.querySelectorAll('.variant-card').forEach(card => {
    const active = card === button;
    card.classList.toggle('active', active);
    card.setAttribute('aria-pressed', String(active));
  });
  if (state.screen === 'game') newGame();
}));

document.querySelectorAll('.mode-chip').forEach(button => button.addEventListener('click', () => {
  state.mode = button.dataset.mode;
  document.querySelectorAll('.mode-chip').forEach(chip => {
    const active = chip === button;
    chip.classList.toggle('active', active);
    chip.setAttribute('aria-pressed', String(active));
  });
  if (state.screen === 'game') newGame();
}));

document.addEventListener('keydown', event => {
  if (/^[1-9]$/.test(event.key) && !event.repeat && !event.ctrlKey && !event.altKey && !event.metaKey && state.screen === 'game') {
    play(Number(event.key) - 1);
  }
});

const initialRoute = location.hash.slice(1);
if (['login', 'register'].includes(initialRoute)) showScreen('auth', initialRoute, false);
else if (initialRoute === 'game') startGame();
else showScreen('home', 'login', false);
refreshAccount().catch(() => {});
renderProgress();
