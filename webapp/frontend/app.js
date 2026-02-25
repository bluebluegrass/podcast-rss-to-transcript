const form = document.getElementById('transcribeForm');
const statusEl = document.getElementById('status');
const resultSection = document.getElementById('resultSection');
const resultTitle = document.getElementById('resultTitle');
const metaEl = document.getElementById('meta');
const transcriptOutput = document.getElementById('transcriptOutput');
const submitBtn = document.getElementById('submitBtn');
const copyBtn = document.getElementById('copyBtn');
const downloadBtn = document.getElementById('downloadBtn');

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

form.addEventListener('submit', async (e) => {
  e.preventDefault();

  const feedUrl = document.getElementById('feedUrl').value.trim();
  const episodeTitle = document.getElementById('episodeTitle').value.trim();
  const includeSpeakers = document.getElementById('includeSpeakers').checked;
  const formatReadable = document.getElementById('formatReadable').checked;

  if (!feedUrl || !episodeTitle) {
    setStatus('RSS feed URL and episode title are required.', 'error');
    return;
  }

  const stage = formatReadable
    ? 'Resolving episode, downloading audio, transcribing, and formatting for readability...'
    : 'Resolving episode, downloading audio, and transcribing...';
  setLoading(true);
  setStatus(stage, 'info');

  try {
    const res = await fetch('/api/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        feed_url: feedUrl,
        episode_title: episodeTitle,
        include_speakers: includeSpeakers,
        format_readable: formatReadable,
      }),
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || 'Failed to generate transcript');
    }

    latestMarkdown = data.transcript_markdown;
    latestFilename = data.suggested_filename || 'transcript.md';

    resultTitle.textContent = data.episode_title || 'Transcript';
    metaEl.textContent = `Mode: ${data.mode} | Readable: ${data.readability_formatted ? 'yes' : 'no'} | Published: ${data.published || 'N/A'} | GUID: ${data.guid}`;
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
