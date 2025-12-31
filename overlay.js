let SERVER_HOST = "martial-karl-microwave-lets.trycloudflare.com";
const WS_PATH = "/ws";

let ws = null;

// ✅ We no longer ask username with prompt.
// We identify the viewer using Twitch Extension auth userId (numeric string).
let myTwitchUserId = null;

let roundActive = false;
let roundEndTime = null;

const NO_VOTE_EMOJI = "😶";

// Automatically identify viewer via Twitch extension
if (window.Twitch && window.Twitch.ext) {
  window.Twitch.ext.onAuthorized((auth) => {
    console.log("Extension Authorized", auth);

    // ✅ This is the real Twitch userId when identity is enabled.
    // If overlay is opened outside of Twitch extension context, it can be null.
    if (auth && auth.userId) {
      myTwitchUserId = String(auth.userId);
      console.log("myTwitchUserId:", myTwitchUserId);
    }
  });
}

function connectWS() {
  const host = SERVER_HOST.replace(/^https?:\/\//i, "").split("/")[0];
  ws = new WebSocket(`wss://${host}${WS_PATH}`);

  ws.onopen = () => {
    document.getElementById("dot").className = "dot ok";
    document.getElementById("status-text").textContent = "CONNECTED";

    // ✅ tell server who we are (no username prompt)
    ws.send(
      JSON.stringify({
        type: "overlay_hello",
        want_state: true,
        twitch_user_id: myTwitchUserId,
      })
    );
  };

  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    handleMessage(data);
  };

  ws.onclose = () => {
    document.getElementById("dot").className = "dot";
    setTimeout(connectWS, 2000);
  };
}

function handleMessage(data) {
  if (data.type === "round_start") {
    roundActive = true;
    roundEndTime = Date.now() + data.duration * 1000;

    document.getElementById("winner-banner").classList.remove("show");
    document.getElementById("mouse-zone").classList.remove("active");

    renderItems(data.options, {});
  } else if (data.type === "vote") {
    const voteEl = document.querySelector(`[data-key="${data.item}"] .votes`);
    if (voteEl) {
      const c = Number(data.count || 0);
      voteEl.textContent = c === 0 ? `${NO_VOTE_EMOJI} 0 VOTES` : `${c} VOTES`;
    }
  } else if (data.type === "placement_request") {
    const banner = document.getElementById("winner-banner");

    document.getElementById("winner-user").textContent = `@${data.chosen_user}`;
    document.getElementById("winner-item-text").textContent =
      `IS PLACING ${data.emoji} ${data.label} — choose: !place left/middle/right or click`;

    banner.classList.add("show");

    // ✅ Unlock mouse ONLY for the winner
    const winnerId = data.chosen_user_id ? String(data.chosen_user_id) : null;
    const isWinner =
      myTwitchUserId && winnerId && String(myTwitchUserId) === String(winnerId);

    if (isWinner) {
      document.getElementById("mouse-zone").classList.add("active");
    } else {
      document.getElementById("mouse-zone").classList.remove("active");
    }
  } else if (data.type === "place_update") {
    // Everyone sees the chosen place
    const banner = document.getElementById("winner-banner");
    const place = (data.place || "").toUpperCase();

    document.getElementById("winner-user").textContent = `@${data.chosen_user}`;
    document.getElementById("winner-item-text").textContent = `CHOSEN PLACE: ${place}`;

    banner.classList.add("show");

    // Winner controls stop after placement
    document.getElementById("mouse-zone").classList.remove("active");
  } else if (data.type === "placement_complete") {
    // Just in case: hide mouse controls
    document.getElementById("mouse-zone").classList.remove("active");
  } else if (data.type === "state" || data.type === "sync") {
    if (data.round) {
      roundActive = !!data.round.active;
      roundEndTime = Date.now() + data.round.duration_remaining * 1000;
    }
    renderItems(data.options, data.votes || {});
  }
}

function renderItems(options, votes) {
  const container = document.getElementById("items");
  if (!options) return;

  container.innerHTML = "";
  options.forEach((opt) => {
    const count = votes[opt.key] || 0;
    const div = document.createElement("div");
    div.className = "item";
    div.setAttribute("data-key", opt.key);

    const voteText = count === 0 ? `${NO_VOTE_EMOJI} 0 VOTES` : `${count} VOTES`;

    div.innerHTML = `
      <div class="emoji">${opt.emoji}</div>
      <div class="label">${opt.label}</div>
      <div class="votes">${voteText}</div>
    `;
    container.appendChild(div);
  });
}

function initMouse() {
  const zone = document.getElementById("mouse-zone");
  let lastX = null,
    lastY = null;

  zone.addEventListener("mousemove", (e) => {
    // ✅ Only winner (active) can send mouse data
    if (!zone.classList.contains("active")) return;

    const rect = zone.getBoundingClientRect();
    const gameX = Math.round(((e.clientX - rect.left) / rect.width) * 960);
    const gameY = Math.round(((e.clientY - rect.top) / rect.height) * 640);

    if (lastX !== null) {
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;

      document.getElementById("xy").textContent = `${gameX}, ${gameY}`;
      document.getElementById("vel").textContent = `${Math.round(dx)}, ${Math.round(dy)}`;

      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            type: "mouse_event",
            x: gameX,
            y: gameY,
            vx: dx,
            vy: dy,
            terminate: false,
          })
        );
      }
    }

    lastX = e.clientX;
    lastY = e.clientY;
  });

  zone.addEventListener("mousedown", (e) => {
    // ✅ Only winner can choose place by click
    if (!zone.classList.contains("active")) return;

    const rect = zone.getBoundingClientRect();
    const relX = (e.clientX - rect.left) / rect.width;

    let place = "middle";
    if (relX < 1 / 3) place = "left";
    else if (relX > 2 / 3) place = "right";

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "place_choice", place }));
    }

    zone.classList.remove("active");
  });
}

function updateTimer() {
  const timerDiv = document.getElementById("timer");
  if (roundActive && roundEndTime) {
    const rem = Math.max(0, Math.ceil((roundEndTime - Date.now()) / 1000));
    timerDiv.textContent = `TIME: ${rem}s`;
    if (rem === 0) roundActive = false;
  } else {
    timerDiv.textContent = "WAITING...";
  }
  requestAnimationFrame(updateTimer);
}

window.addEventListener("DOMContentLoaded", () => {
  connectWS();
  initMouse();
  updateTimer();
});
