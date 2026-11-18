/* Inventory page logic — extracted from inventory.html (no Jinja variables). */
var allCampaigns = [];
var viewMode = 'my';
var currentPriority = [];
var settingsLoaded = false;
var currentExclude = [];

async function fetchProgress() {
  try {
    const resp = await fetch('/api/progress');
    const data = await resp.json();

    const statusEl = document.getElementById('mining-status');
    const infoEl = document.getElementById('mining-info');

    if (!data.mining) {
      statusEl.textContent = 'Inactive';
      statusEl.className = 'status-inactive';
      infoEl.textContent = 'Miner not running';
      document.getElementById('current-campaign').textContent = '-';
      document.getElementById('current-game').textContent = '-';
      document.getElementById('campaign-progress-bar').style.width = '0%';
      document.getElementById('campaign-time-info').textContent = '-';
    } else if (data.waiting_campaigns) {
      statusEl.textContent = 'Waiting';
      statusEl.className = 'status-inactive';
      infoEl.textContent = 'No active campaigns to mine';
      document.getElementById('current-campaign').textContent = '-';
      document.getElementById('current-game').textContent = '-';
      document.getElementById('campaign-progress-bar').style.width = '0%';
      document.getElementById('campaign-time-info').textContent = '-';
    } else if (data.currently_mining) {
      statusEl.textContent = 'Active';
      statusEl.className = 'status-active';
      const dropCount = Object.keys(data.drops || {}).length;
      infoEl.textContent = 'Mining ' + dropCount + ' drop' + (dropCount !== 1 ? 's' : '');

      if (data.current_campaign) {
        document.getElementById('current-campaign').textContent = data.current_campaign.name || '-';
        document.getElementById('current-game').textContent = data.current_campaign.game || '-';
        const progress = (data.current_campaign.current_minutes / data.current_campaign.required_minutes) * 100;
        document.getElementById('campaign-progress-bar').style.width = Math.min(progress, 100) + '%';
        document.getElementById('campaign-time-info').textContent =
          (data.current_campaign.current_minutes || 0) + ' / ' + (data.current_campaign.required_minutes || 0) + ' min';
      }
    } else {
      statusEl.textContent = 'Idle';
      statusEl.className = 'status-inactive';
      infoEl.textContent = 'Waiting for channels to mine';
    }
  } catch (e) {
    console.error('Progress fetch error:', e);
  }
}

async function fetchSettings() {
  try {
    const resp = await fetch('/api/settings');
    const data = await resp.json();

    viewMode = data.inventory_view_mode || 'my';
    document.getElementById('btn-my-campaigns').classList.toggle('active', viewMode === 'my');
    document.getElementById('btn-my-campaigns').classList.toggle('btn-outline-secondary', viewMode !== 'my');
    document.getElementById('btn-all-campaigns').classList.toggle('active', viewMode === 'all');
    document.getElementById('btn-all-campaigns').classList.toggle('btn-outline-secondary', viewMode !== 'all');
    syncOnlyMineState();

    const filters = data.inventory_filters || {active: false, upcoming: true, expired: false, not_linked: false, excluded: false, finished: false};
    document.getElementById('f-active').checked = filters.active;
    document.getElementById('f-upcoming').checked = filters.upcoming;
    document.getElementById('f-expired').checked = filters.expired;
    document.getElementById('f-not_linked').checked = filters.not_linked;
    document.getElementById('f-excluded').checked = filters.excluded;
    document.getElementById('f-finished').checked = filters.finished;

    currentExclude = data.exclude || [];
    currentPriority = data.priority || [];
    const onlyMineEl = document.getElementById('f-only-mine');
    if (onlyMineEl) onlyMineEl.checked = data.inventory_only_mine !== false;
    settingsLoaded = true;
  } catch (e) {
    console.error('Settings fetch error:', e);
    settingsLoaded = true;
  }
}

async function fetchInventory() {
  try {
    const resp = await fetch('/api/inventory');
    const data = await resp.json();

    if (Array.isArray(data)) {
      allCampaigns = data;
    } else if (data.campaigns) {
      allCampaigns = data.campaigns;
    } else {
      allCampaigns = [];
    }
    renderCampaigns();
    updateAddButtons();
    updateExcludeButtons();
  } catch (e) {
    console.error('Inventory fetch error:', e);
    document.getElementById('campaigns-container').innerHTML =
      '<div class="text-center text-muted p-4">Error loading campaigns</div>';
  }
}

async function fetchCurrentPriority() {
  try {
    const resp = await fetch('/api/settings');
    const data = await resp.json();
    currentPriority = data.priority || [];
    updateAddButtons();
  } catch (e) {
    console.error('Error fetching priority:', e);
  }
}

function setViewMode(mode) {
  viewMode = mode;
  document.getElementById('btn-my-campaigns').classList.toggle('active', mode === 'my');
  document.getElementById('btn-my-campaigns').classList.toggle('btn-outline-secondary', mode !== 'my');
  document.getElementById('btn-all-campaigns').classList.toggle('active', mode === 'all');
  document.getElementById('btn-all-campaigns').classList.toggle('btn-outline-secondary', mode !== 'all');
  syncOnlyMineState();
  renderCampaigns();
  saveInventorySettings();
}

function syncOnlyMineState() {
  const el = document.getElementById('f-only-mine');
  if (!el) return;
  const isMy = viewMode === 'my';
  el.disabled = !isMy;
  const lbl = document.querySelector('label[for="f-only-mine"]');
  if (lbl) {
    lbl.style.opacity = isMy ? '1' : '.45';
    lbl.style.cursor = isMy ? 'pointer' : 'not-allowed';
    lbl.title = isMy
      ? 'Show only games in your Priority list, minus Excluded'
      : 'Only applies to My Campaigns';
  }
}

function updateFilters() {
  renderCampaigns();
  saveInventorySettings();
}

async function saveInventorySettings() {
  if (!settingsLoaded) return;
  const filters = {
    active: document.getElementById('f-active').checked,
    upcoming: document.getElementById('f-upcoming').checked,
    expired: document.getElementById('f-expired').checked,
    not_linked: document.getElementById('f-not_linked').checked,
    excluded: document.getElementById('f-excluded').checked,
    finished: document.getElementById('f-finished').checked
  };
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        inventory_view_mode: viewMode,
        inventory_filters: filters,
        inventory_only_mine: document.getElementById('f-only-mine') ? document.getElementById('f-only-mine').checked : true
      })
    });
  } catch (e) {
    console.error('Error saving inventory settings:', e);
  }
}

function shouldShowCampaign(campaign) {
  const gameName = String(campaign.game?.name || campaign.name || '').trim().toLowerCase();

  // "Only my games": keep games from the Priority list, minus the Excluded list.
  // Only applies in "My Campaigns" view; "All Twitch" ignores the user lists.
  const onlyMineEl = document.getElementById('f-only-mine');
  if (viewMode === 'my' && onlyMineEl && onlyMineEl.checked) {
    const excludeNames = (currentExclude || []).map(function (e) { return String(e).trim().toLowerCase(); });
    if (gameName && excludeNames.indexOf(gameName) !== -1) return false;
    const priorityNames = (currentPriority || []).map(function (p) { return String(p).trim().toLowerCase(); });
    // If the Priority list is empty, do not hide everything; only apply exclusions.
    if (priorityNames.length > 0 && gameName && priorityNames.indexOf(gameName) === -1) return false;
  }

  const filters = {
    'f-active': campaign.active,
    'f-upcoming': campaign.upcoming,
    'f-expired': campaign.expired,
    'f-not_linked': !campaign.linked,
    'f-excluded': campaign.game && campaign.game.excluded,
    'f-finished': campaign.finished
  };
  for (const [id, value] of Object.entries(filters)) {
    if (document.getElementById(id).checked && value) return true;
  }
  return false;
}

function renderCampaigns() {
  const container = document.getElementById('campaigns-container');

  if (!allCampaigns || allCampaigns.length === 0) {
    container.innerHTML = '<div class="text-center text-muted p-4">No campaigns found</div>';
    return;
  }

  let html = '';

  allCampaigns.forEach(campaign => {
    if (!shouldShowCampaign(campaign)) return;

    const statusClass = campaign.active ? 'campaign-status-active' :
                       campaign.upcoming ? 'campaign-status-upcoming' : 'campaign-status-expired';
    const statusText = campaign.active ? 'Active' : campaign.upcoming ? 'Upcoming' : 'Expired';

    let dropsHtml = '';
    if (campaign.drops && campaign.drops.length > 0) {
      campaign.drops.forEach(drop => {
        const progressPct = (drop.progress || 0) * 100;
        const dropStatus = drop.is_claimed ? 'Claimed' :
                         drop.can_claim ? 'Can Claim' :
                         progressPct >= 100 ? 'Ready' : progressPct.toFixed(1) + '%';
        const dropStatusColor = drop.is_claimed ? '#00ff88' :
                              drop.can_claim ? '#ffaa00' : '#e0e0e0';

        let benefitImagesHtml = '';
        if (drop.benefit_images && drop.benefit_images.length > 0) {
          benefitImagesHtml = drop.benefit_images.slice(0, 3).map(img =>
            `<img class="benefit-img" src="${img}" onclick="showImageModal('${img}', '${(drop.name || '').replace(/'/g, "\\'")}', '${(campaign.game?.name || '').replace(/'/g, "\\'")}', '${(campaign.name || '').replace(/'/g, "\\'")}')" onerror="this.style.display='none'">`
          ).join('');
        } else if (drop.image_url) {
          benefitImagesHtml = `<img class="benefit-img" src="${drop.image_url}" onclick="showImageModal('${drop.image_url}', '${(drop.name || '').replace(/'/g, "\\'")}', '${(campaign.game?.name || '').replace(/'/g, "\\'")}', '${(campaign.name || '').replace(/'/g, "\\'")}')" onerror="this.style.display='none'">`;
        }

        dropsHtml += `
          <div class="drop-item">
            ${benefitImagesHtml}
            <div style="flex: 1">
              <div style="font-size: 0.8rem;">${drop.name || 'Unknown Drop'}</div>
              <div class="progress-bar-custom" style="margin-top: 4px;">
                <div class="progress-bar-fill" style="width: ${progressPct}%"></div>
              </div>
            </div>
            <span style="font-size: 0.75rem; color: ${dropStatusColor};">${dropStatus}</span>
          </div>
        `;
      });
    }

    html += `
      <div class="campaign-card" data-game-id="${campaign.game?.id || ''}">
        <div class="d-flex gap-3">
          ${campaign.image_url ? `<img class="campaign-art" src="${campaign.image_url}" onclick="showImageModal('${campaign.image_url}', '${(campaign.game?.name || campaign.name || '').replace(/'/g, "\\'")}', '${campaign.game?.name || ''}', '${campaign.name || ''}')" onerror="this.style.display='none'">` : '<div style="width:60px;height:80px;background:#26262e;border-radius:4px;"></div>'}
          <div style="flex: 1">
            <div class="d-flex justify-content-between align-items-start mb-2">
              <div>
                <div class="game-name">${campaign.game?.name || campaign.name || 'Unknown Game'}</div>
                <div class="campaign-time">${campaign.name || ''}</div>
              </div>
              <div class="d-flex align-items-center gap-2">
                <span class="${statusClass}" style="font-size: 0.75rem;">${statusText}</span>
                ${!campaign.linked ? `<a href="${campaign.link_url || 'https://www.twitch.tv/drops/campaigns'}" target="_blank" style="font-size: 0.75rem; color: #ff4444; text-decoration: underline;">Not Linked</a>` : ''}
                <button class="btn btn-quit" onclick="addToExclude('${campaign.game?.id || ''}', '${(campaign.game?.name || '').replace(/'/g, "\\'")}')">Quit</button>
                <button class="btn btn-add" onclick="addToPriority('${campaign.game?.id || ''}', '${(campaign.game?.name || '').replace(/'/g, "\\'")}')">Add</button>
              </div>
            </div>
            ${dropsHtml}
            <div class="campaign-time mt-2">
              ${campaign.starts_at ? 'Start: ' + new Date(campaign.starts_at).toLocaleString() : ''}
              ${campaign.ends_at ? ' | End: ' + new Date(campaign.ends_at).toLocaleString() : ''}
            </div>
          </div>
        </div>
      </div>
    `;
  });

  container.innerHTML = html || '<div class="text-center text-muted p-4">No campaigns match filters</div>';
}

async function addToPriority(gameId, gameName, quiet) {
  try {
    const resp = await fetch('/api/priority/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({game_id: gameId, game_name: gameName})
    });
    const data = await resp.json();
    if (data.success) {
      currentPriority = data.priority || [];
      if (quiet) return;
      renderCampaigns();
      updateAddButtons();
      updateExcludeButtons();
      if (window.DNXWebUI) DNXWebUI.toast('Added to Priority', 'ok');
    }
  } catch (e) {
    console.error('Error adding to priority:', e);
  }
}

async function addToExclude(gameId, gameName) {
  try {
    const resp = await fetch('/api/exclude/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({game_id: gameId, game_name: gameName})
    });
    const data = await resp.json();
    if (data.success) {
      currentExclude = data.exclude || [];
      renderCampaigns();
      updateAddButtons();
      updateExcludeButtons();
      if (window.DNXWebUI) DNXWebUI.toast('Added to Excluded', 'ok');
    }
  } catch (e) {
    console.error('Error adding to exclude:', e);
  }
}

function updateExcludeButtons() {
  document.querySelectorAll('.campaign-card').forEach(card => {
    const gameNameEl = card.querySelector('.game-name');
    const btn = card.querySelector('.btn-quit');
    if (!btn || !gameNameEl) return;
    const gameName = gameNameEl.textContent.trim().toLowerCase();
    const isExcluded = (currentExclude || []).some(e => String(e).trim().toLowerCase() === gameName);
    btn.disabled = isExcluded;
    btn.textContent = isExcluded ? 'Excluded' : 'Quit';
  });
}

function updateAddButtons() {
  document.querySelectorAll('.campaign-card').forEach(card => {
    const gameNameEl = card.querySelector('.game-name');
    const btn = card.querySelector('.btn-add');
    if (!btn || !gameNameEl) return;
    const gameName = gameNameEl.textContent.trim().toLowerCase();
    const isInPriority = (currentPriority || []).some(p => String(p).trim().toLowerCase() === gameName);
    btn.disabled = isInPriority;
    btn.textContent = isInPriority ? 'Added' : 'Add';
  });
}

async function restartMiner() {
  const btn = document.querySelector('.restart-btn');
  if (btn) btn.disabled = true;
  await fetch('/api/restart', {method: 'POST'});
  setTimeout(function () {
    if (btn) btn.disabled = false;
    fetchProgress();
  }, 3000);
}

function showImageModal(src, name, gameName, campaignName) {
  const sub = (gameName || '') + (campaignName ? ' - ' + campaignName : '');
  if (window.DNXWebUI && DNXWebUI.openImage) {
    DNXWebUI.openImage(src, name || '', sub);
  }
}

function closeImageModal() {
  if (window.DNXWebUI && DNXWebUI.closeImage) DNXWebUI.closeImage();
}

document.addEventListener('DOMContentLoaded', function () {
  fetchSettings().then(function () {
    fetchProgress();
    fetchInventory();
  });

  setInterval(fetchProgress, 30000);
  setInterval(fetchInventory, 30000);
});

document.addEventListener('dnx:refresh', function () {
  fetchProgress();
  fetchInventory();
});
