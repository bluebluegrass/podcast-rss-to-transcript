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
let latestJobId = '';

function setStatus(message, type = 'info') {
  statusEl.textContent = message;
  statusEl.dataset.type = type;
}

function setLoading(loading) {
  submitBtn.disabled = loading;
  submitBtn.textContent = loading ? 'Job Running...' : 'Generate Transcript';
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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function renderResult(data) {
  latestMarkdown = data.transcript_markdown;
  latestFilename = data.suggested_filename || 'transcript.md';

  resultTitle.textContent = data.episode_title || 'Transcript';
  const warningsText = Array.isArray(data.warnings) && data.warnings.length
    ? data.warnings.join(' | ')
    : 'none';

  const durationText = Number.isFinite(data.audio_duration_seconds)
    ? `${Math.round(data.audio_duration_seconds)}s`
    : 'N/A';

  metaEl.textContent = [
    `Mode: ${data.mode}`,
    `Readable: ${data.readability_formatted ? 'yes' : 'no'}`,
    `Published: ${data.published || 'N/A'}`,
    `Discovery: ${data.discovery_method || 'N/A'}`,
    `Resolved feed: ${data.resolved_feed_url || 'N/A'}`,
    `Podcast resolved: ${data.podcast_title_resolved || 'N/A'}`,
    `GUID: ${data.guid}`,
    `Duration: ${durationText}`,
    `Chunks: ${data.chunk_count || 1} @ ${data.chunk_seconds || 0}s`,
    `Warnings: ${warningsText}`,
  ].join(' | ');

  transcriptOutput.value = data.transcript_markdown;
  resultSection.classList.remove('hidden');
}

async function createJob(payload) {
  const res = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.detail || 'Failed to create transcription job');
  }

  return data;
}

async function pollJob(jobId) {
  while (true) {
    const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.detail || 'Failed to fetch job status');
    }

    const progress = Number.isFinite(data.progress_percent)
      ? ` (${data.progress_percent}%)`
      : '';

    if (data.status === 'queued') {
      setStatus(`Queued: ${data.progress_stage || 'Waiting for worker'}${progress}`, 'info');
    } else if (data.status === 'running') {
      setStatus(`${data.progress_stage || 'Processing'}${progress}`, 'info');
    } else if (data.status === 'failed') {
      throw new Error(data.error || 'Transcription job failed');
    } else if (data.status === 'completed') {
      if (!data.result) {
        throw new Error('Job completed but transcript payload is missing');
      }
      renderResult(data.result);
      setStatus('Transcript generated successfully.', 'success');
      return;
    }

    await sleep(2000);
  }
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

  setLoading(true);
  resultSection.classList.add('hidden');
  latestMarkdown = '';
  latestFilename = 'transcript.md';
  latestJobId = '';

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

    const created = await createJob(payload);
    latestJobId = created.job_id;
    setStatus(`Job queued (${latestJobId}). Starting soon...`, 'info');

    await pollJob(latestJobId);
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
  if (!latestJobId) {
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
    return;
  }

  const a = document.createElement('a');
  a.href = `/api/jobs/${encodeURIComponent(latestJobId)}/download`;
  a.download = latestFilename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setStatus('Downloading transcript file.', 'success');
});
