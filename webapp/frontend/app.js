const form = document.getElementById('transcribeForm');
const statusEl = document.getElementById('status');
const resultSection = document.getElementById('resultSection');
const resultTitle = document.getElementById('resultTitle');
const metaEl = document.getElementById('meta');
const transcriptOutput = document.getElementById('transcriptOutput');
const submitBtn = document.getElementById('submitBtn');
const copyBtn = document.getElementById('copyBtn');
const downloadBtn = document.getElementById('downloadBtn');

const modeRss = document.getElementById('modeRss');
const modePodcast = document.getElementById('modePodcast');
const feedGroup = document.getElementById('feedGroup');
const podcastGroup = document.getElementById('podcastGroup');
const feedUrlInput = document.getElementById('feedUrl');
const podcastTitleInput = document.getElementById('podcastTitle');
const episodeTitleInput = document.getElementById('episodeTitle');

let latestMarkdown = '';
let latestFilename = 'transcript.md';

function setStatus(message, type = 'info') {
  statusEl.textContent = message;
  statusEl.dataset.type = type;
}

function setLoading(loading) {
  submitBtn.disabled = loading;
  submitBtn.textContent = loading ? 'Working...' : 'Generate Transcript';
}

function currentMode() {
  return modePodcast.checked ? 'podcast' : 'rss';
}

function applyModeVisibility() {
  const mode = currentMode();
  if (mode === 'rss') {
    feedGroup.classList.remove('hidden');
    podcastGroup.classList.add('hidden');
    feedUrlInput.required = true;
    podcastTitleInput.required = false;
    return;
  }

  feedGroup.classList.add('hidden');
  podcastGroup.classList.remove('hidden');
  feedUrlInput.required = false;
  podcastTitleInput.required = true;
}

modeRss.addEventListener('change', applyModeVisibility);
modePodcast.addEventListener('change', applyModeVisibility);
applyModeVisibility();

form.addEventListener('submit', async (e) => {
  e.preventDefault();

  const mode = currentMode();
  const feedUrl = feedUrlInput.value.trim();
  const podcastTitle = podcastTitleInput.value.trim();
  const episodeTitle = episodeTitleInput.value.trim();
  const includeSpeakers = document.getElementById('includeSpeakers').checked;
  const formatReadable = document.getElementById('formatReadable').checked;

  if (!episodeTitle) {
    setStatus('Episode title is required.', 'error');
    return;
  }

  if (mode === 'rss' && !feedUrl) {
    setStatus('RSS feed URL is required in RSS mode.', 'error');
    return;
  }

  if (mode === 'podcast' && !podcastTitle) {
    setStatus('Podcast title is required in podcast-title mode.', 'error');
    return;
  }

  const stageBase = mode === 'podcast'
    ? 'Searching podcast feed, resolving episode, downloading audio, and transcribing...'
    : 'Resolving episode, downloading audio, and transcribing...';

  const stage = formatReadable
    ? `${stageBase} Then formatting for readability...`
    : stageBase;

  setLoading(true);
  setStatus(stage, 'info');

  try {
    const payload = {
      episode_title: episodeTitle,
      include_speakers: includeSpeakers,
      format_readable: formatReadable,
    };

    if (mode === 'rss') {
      payload.feed_url = feedUrl;
    } else {
      payload.podcast_title = podcastTitle;
    }

    const res = await fetch('/api/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || 'Failed to generate transcript');
    }

    latestMarkdown = data.transcript_markdown;
    latestFilename = data.suggested_filename || 'transcript.md';

    resultTitle.textContent = data.episode_title || 'Transcript';
    const warningsText = Array.isArray(data.warnings) && data.warnings.length
      ? data.warnings.join(' | ')
      : 'none';

    metaEl.textContent = [
      `Mode: ${data.mode}`,
      `Readable: ${data.readability_formatted ? 'yes' : 'no'}`,
      `Published: ${data.published || 'N/A'}`,
      `Discovery: ${data.discovery_method || 'N/A'}`,
      `Resolved feed: ${data.resolved_feed_url || 'N/A'}`,
      `Podcast resolved: ${data.podcast_title_resolved || 'N/A'}`,
      `GUID: ${data.guid}`,
      `Warnings: ${warningsText}`,
    ].join(' | ');

    transcriptOutput.value = data.transcript_markdown;

    resultSection.classList.remove('hidden');
    setStatus('Transcript generated successfully.', 'success');
  } catch (err) {
    setStatus(`Error: ${err.message}`, 'error');
  } finally {
    setLoading(false);
  }
});

copyBtn.addEventListener('click', async () => {
  if (!latestMarkdown) {
    setStatus('No transcript available to copy.', 'error');
    return;
  }
  try {
    await navigator.clipboard.writeText(latestMarkdown);
    setStatus('Copied transcript to clipboard.', 'success');
  } catch (_) {
    setStatus('Clipboard copy failed. Please copy manually.', 'error');
  }
});

downloadBtn.addEventListener('click', () => {
  if (!latestMarkdown) {
    setStatus('No transcript available to download.', 'error');
    return;
  }
  const blob = new Blob([latestMarkdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = latestFilename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setStatus('Downloaded transcript file.', 'success');
});
