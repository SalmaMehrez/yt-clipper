const LOCAL_AGENT_URL = "http://localhost:5000";

// Check agent status on load
window.addEventListener('DOMContentLoaded', async () => {
    const statusBadge = document.getElementById('statusBadge');
    const downloadBtn = document.getElementById('downloadAgentBtn');

    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 2000);
        
        const response = await fetch(`${LOCAL_AGENT_URL}/ping`, { signal: controller.signal });
        clearTimeout(timeoutId);

        if (response.ok) {
            statusBadge.className = 'badge success';
            statusBadge.textContent = '✅ Agent connecté';
            downloadBtn.classList.add('hidden');
        } else {
            throw new Error();
        }
    } catch (err) {
        statusBadge.className = 'badge warning';
        statusBadge.textContent = '⚠️ Agent non détecté';
        downloadBtn.classList.remove('hidden');
    }
});

document.getElementById('checkVideoBtn').addEventListener('click', async function () {
    const urlInput = document.getElementById('url');
    const btn = document.getElementById('checkVideoBtn');
    const statusMessage = document.getElementById('statusMessage');
    const metaContainer = document.getElementById('videoMeta');

    if (!urlInput.value) {
        statusMessage.textContent = "Veuillez entrer une URL d'abord.";
        statusMessage.classList.remove('hidden');
        statusMessage.className = 'error';
        return;
    }

    btn.disabled = true;
    btn.textContent = "⏳...";
    statusMessage.classList.add('hidden');

    try {
        const response = await fetch(`${LOCAL_AGENT_URL}/info`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: urlInput.value })
        });
        const data = await response.json();

        if (response.ok) {
            document.getElementById('metaTitle').textContent = data.title;
            document.getElementById('metaThumb').src = data.thumbnail;
            
            const minutes = Math.floor(data.duration / 60);
            const seconds = data.duration % 60;
            document.getElementById('metaDuration').textContent = `Durée: ${minutes}m ${seconds}s`;

            metaContainer.classList.remove('hidden');
            metaContainer.style.display = 'flex';
        } else {
            throw new Error(data.error || "Erreur lors de la récupération des infos.");
        }
    } catch (error) {
        statusMessage.textContent = "Erreur: Assurez-vous que l'agent local est lancé. " + error.message;
        statusMessage.className = 'error';
        statusMessage.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.textContent = "🔍 Vérifier";
    }
});

document.getElementById('clipForm').addEventListener('submit', async function (e) {
    e.preventDefault();

    const submitBtn = document.getElementById('submitBtn');
    const statusMessage = document.getElementById('statusMessage');
    const resultArea = document.getElementById('resultArea');
    const downloadLink = document.getElementById('downloadLink');

    statusMessage.classList.add('hidden');
    statusMessage.className = 'hidden';
    resultArea.classList.add('hidden');

    submitBtn.disabled = true;
    submitBtn.textContent = '⏳ Téléchargement et découpage en cours...';

    const payload = {
        url: document.getElementById('url').value,
        start: document.getElementById('startTime').value,
        end: document.getElementById('endTime').value
    };

    try {
        const response = await fetch(`${LOCAL_AGENT_URL}/clip`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            const blob = await response.blob();
            const downloadUrl = window.URL.createObjectURL(blob);
            
            statusMessage.textContent = '✅ Clip téléchargé avec succès !';
            statusMessage.className = 'success';
            statusMessage.classList.remove('hidden');

            downloadLink.href = downloadUrl;
            downloadLink.download = `clip_${Date.now()}.mp4`;
            resultArea.classList.remove('hidden');
            
            // Trigger automatic download
            downloadLink.click();
        } else {
            const data = await response.json();
            throw new Error(data.error || 'Une erreur est survenue.');
        }
    } catch (error) {
        statusMessage.textContent = error.message;
        statusMessage.className = 'error';
        statusMessage.classList.remove('hidden');
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Extraire la séquence';
    }
});
