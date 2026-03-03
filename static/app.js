document.getElementById('checkVideoBtn').addEventListener('click', async function () {
    const urlInput = document.getElementById('url');
    const btn = document.getElementById('checkVideoBtn');
    const statusMessage = document.getElementById('statusMessage');
    const qualitySelect = document.getElementById('quality');
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

    const formData = new FormData();
    formData.append('url', urlInput.value);

    try {
        const response = await fetch('/api/info', {
            method: 'POST',
            body: formData
        });
        const data = await response.json();

        if (response.ok) {
            // Update Metadata UI
            document.getElementById('metaTitle').textContent = data.title;
            document.getElementById('metaThumb').src = data.thumbnail;

            // Format duration
            const minutes = Math.floor(data.duration / 60);
            const seconds = data.duration % 60;
            document.getElementById('metaDuration').textContent = `Durée: ${minutes}m ${seconds}s`;

            metaContainer.classList.remove('hidden');
            metaContainer.style.display = 'flex'; // Ensure flex layout

            // Update Quality Dropdown
            qualitySelect.innerHTML = ''; // Clear existing
            data.qualities.forEach(q => {
                const option = document.createElement('option');
                option.value = q.value;
                option.textContent = q.label;
                qualitySelect.appendChild(option);
            });

            // Auto-fill duration if empty (optional, helpful for full clip)
            // document.getElementById('endTime').value = data.duration; 

        } else {
            throw new Error(data.detail || "Erreur lors de la récupération des infos.");
        }
    } catch (error) {
        statusMessage.textContent = error.message;
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

    // Reset UI
    statusMessage.classList.add('hidden');
    statusMessage.className = 'hidden'; // Remove error/success classes
    resultArea.classList.add('hidden');

    // Clear previous dynamic info
    const existingRes = document.querySelector('.video-info p:nth-child(3)');
    if (existingRes) existingRes.remove();

    document.getElementById('videoPlayer').pause();
    document.getElementById('videoPlayer').src = "";
    document.getElementById('videoPlayer').classList.add('hidden');
    submitBtn.disabled = true;
    submitBtn.textContent = 'Traitement en cours...';

    const formData = new FormData();
    formData.append('url', document.getElementById('url').value);
    formData.append('quality', document.getElementById('quality').value);
    formData.append('start_time', document.getElementById('startTime').value);
    formData.append('end_time', document.getElementById('endTime').value);

    try {
        const response = await fetch('/api/clip', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        if (response.ok) {
            statusMessage.textContent = 'Séquence extraite avec succès !';
            statusMessage.className = 'success';
            statusMessage.classList.remove('hidden');

            document.getElementById('videoTitle').textContent = data.title || 'Votre vidéo';
            document.getElementById('videoDuration').textContent = data.duration;

            const videoPlayer = document.getElementById('videoPlayer');
            videoPlayer.src = data.download_url;
            videoPlayer.classList.remove('hidden');

            downloadLink.href = data.download_url;
            resultArea.classList.remove('hidden');
        } else {
            throw new Error(data.detail || 'Une erreur est survenue.');
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
