(() => {
    const EXCLUDED_FOLDERS = new Set(["processed", "quarantined"]);

    // State
    let queue = []; // { id, file, relativePath, status, detail, docId }
    let nextId = 1;
    let isUploading = false;
    let shouldStop = false;
    let activeAbortController = null;
    let maxUploadMb = 100;

    // DOM refs
    const connectionSummary = document.getElementById("connection-summary");
    const connectionStatus = document.getElementById("connection-status");
    const checkConnectionBtn = document.getElementById("check-connection-btn");

    const dropzone = document.getElementById("dropzone");
    const browseFolderBtn = document.getElementById("browse-folder-btn");
    const browseFilesBtn = document.getElementById("browse-files-btn");
    const folderInput = document.getElementById("folder-input");
    const filesInput = document.getElementById("files-input");

    const recursiveCheck = document.getElementById("recursive-check");
    const excludeProcessedCheck = document.getElementById("exclude-processed-check");
    const dedupManifestCheck = document.getElementById("dedup-manifest-check");
    const dedupCatalogCheck = document.getElementById("dedup-catalog-check");
    const docTypeSelect = document.getElementById("doc-type-select");

    const selectionStatus = document.getElementById("selection-status");
    const queueContainer = document.getElementById("queue-container");
    const queueSummary = document.getElementById("queue-summary");
    const queueTbody = document.getElementById("queue-tbody");

    const startUploadBtn = document.getElementById("start-upload-btn");
    const stopUploadBtn = document.getElementById("stop-upload-btn");
    const retryFailedBtn = document.getElementById("retry-failed-btn");
    const clearQueueBtn = document.getElementById("clear-queue-btn");

    const currentFileEl = document.getElementById("current-file");
    const progressFill = document.getElementById("progress-fill");
    const statUploaded = document.getElementById("stat-uploaded");
    const statSkipped = document.getElementById("stat-skipped");
    const statFailed = document.getElementById("stat-failed");
    const statRemaining = document.getElementById("stat-remaining");
    const uploadStatus = document.getElementById("upload-status");

    const logWindow = document.getElementById("log-window");
    const clearLogBtn = document.getElementById("clear-log-btn");
    const logoutWrap = document.getElementById("logout-wrap");

    // --- Helpers ---
    function formatBytes(bytes) {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }

    function showBanner(el, message, type = "info") {
        el.textContent = message;
        el.className = `status ${type === "info" ? "" : type}`.trim();
        el.classList.remove("hidden");
    }

    function hideBanner(el) {
        el.classList.add("hidden");
    }

    function appendLog(level, message) {
        const ts = new Date().toLocaleTimeString([], { hour12: false });
        const entry = document.createElement("div");
        entry.className = `log-entry log-${level}`;
        entry.textContent = `[${ts}] [${level}] ${message}`;
        logWindow.appendChild(entry);
        logWindow.scrollTop = logWindow.scrollHeight;
    }

    function isExcludedPath(relPath) {
        const parts = relPath.replace(/\\/g, "/").split("/").filter(Boolean);
        // Check directory segments (exclude filename at the end)
        for (let i = 0; i < parts.length - 1; i++) {
            if (EXCLUDED_FOLDERS.has(parts[i].toLowerCase())) {
                return true;
            }
        }
        return false;
    }

    function isNestedSubfolder(relPath) {
        const parts = relPath.replace(/\\/g, "/").split("/").filter(Boolean);
        // webkitdirectory paths look like "RootFolder/file.pdf" (length 2) or "RootFolder/Sub/file.pdf" (length >= 3)
        return parts.length > 2;
    }

    // --- Connection Info ---
    async function loadServerInfo(verifyOAuth = false) {
        if (verifyOAuth) {
            checkConnectionBtn.disabled = true;
            showBanner(connectionStatus, "Testing Mendeley OAuth token refresh...", "info");
        }
        try {
            const resp = await fetch(`api/info?verify=${verifyOAuth ? "true" : "false"}`);
            if (resp.status === 401) {
                window.location.reload();
                return;
            }
            const data = await resp.json();
            maxUploadMb = data.max_upload_mb || 100;
            if (data.auth_enabled) {
                logoutWrap.classList.remove("hidden");
            }
            const ledgerText = `${(data.manifest_count || 0).toLocaleString()} books tracked in manifest ledger`;
            const credText = data.credentials_configured
                ? "Mendeley credentials configured"
                : "Mendeley credentials missing";
            connectionSummary.textContent = `${credText} • ${ledgerText} • Max ${maxUploadMb} MB/PDF`;

            if (verifyOAuth) {
                if (data.mendeley_connected) {
                    showBanner(connectionStatus, "✅ Connected to Mendeley API successfully!", "success");
                    appendLog("SUCCESS", "Verified Mendeley OAuth connection.");
                } else {
                    showBanner(connectionStatus, `❌ Mendeley auth error: ${data.mendeley_error || "Unknown error"}`, "error");
                    appendLog("ERROR", `Mendeley auth verification failed: ${data.mendeley_error}`);
                }
            }
        } catch (err) {
            connectionSummary.textContent = "Unable to reach uploader backend.";
            if (verifyOAuth) {
                showBanner(connectionStatus, `Network error: ${err.message}`, "error");
            }
        } finally {
            checkConnectionBtn.disabled = false;
        }
    }

    // --- Queue Management ---
    function addFilesToQueue(fileEntries) {
        const includeSubfolders = recursiveCheck.checked;
        const skipExcluded = excludeProcessedCheck.checked;
        const existingKeys = new Set(queue.map((item) => `${item.relativePath}::${item.file.size}`));

        let added = 0;
        let ignoredNonPdf = 0;
        let ignoredExcluded = 0;
        let ignoredSubfolder = 0;
        let ignoredAlreadyQueued = 0;

        for (const { file, relativePath } of fileEntries) {
            const rel = (relativePath || file.webkitRelativePath || file.name).replace(/\\/g, "/").replace(/^\/+/, "");
            if (!file.name.toLowerCase().endsWith(".pdf")) {
                ignoredNonPdf++;
                continue;
            }
            if (!includeSubfolders && isNestedSubfolder(rel)) {
                ignoredSubfolder++;
                continue;
            }
            if (skipExcluded && isExcludedPath(rel)) {
                ignoredExcluded++;
                continue;
            }
            const key = `${rel}::${file.size}`;
            if (existingKeys.has(key)) {
                ignoredAlreadyQueued++;
                continue;
            }
            existingKeys.add(key);
            queue.push({
                id: nextId++,
                file,
                relativePath: rel,
                status: "queued",
                detail: "",
                docId: null,
            });
            added++;
        }

        // Sort queued items alphabetically by relative path for predictable order
        queue.sort((a, b) => a.relativePath.localeCompare(b.relativePath));

        const notes = [];
        if (ignoredNonPdf > 0) notes.push(`${ignoredNonPdf} non-PDF ignored`);
        if (ignoredExcluded > 0) notes.push(`${ignoredExcluded} in Processed/Quarantined skipped`);
        if (ignoredSubfolder > 0) notes.push(`${ignoredSubfolder} nested subfolder PDFs skipped`);
        if (ignoredAlreadyQueued > 0) notes.push(`${ignoredAlreadyQueued} already in queue`);

        const noteSuffix = notes.length ? ` (${notes.join(", ")})` : "";
        if (added > 0) {
            showBanner(selectionStatus, `Queued ${added} PDF file(s)${noteSuffix}.`, "success");
            appendLog("INFO", `Added ${added} PDF(s) to queue${noteSuffix}.`);
        } else {
            showBanner(selectionStatus, `No new PDF files were added${noteSuffix}.`, "warning");
        }

        renderQueue();
        updateProgressUI();
    }

    function renderQueue() {
        if (queue.length === 0) {
            queueContainer.classList.add("hidden");
            queueTbody.innerHTML = "";
            return;
        }

        queueContainer.classList.remove("hidden");
        const totalBytes = queue.reduce((sum, item) => sum + item.file.size, 0);
        const folders = new Set(
            queue.map((item) => {
                const idx = item.relativePath.lastIndexOf("/");
                return idx >= 0 ? item.relativePath.slice(0, idx) : "(root)";
            })
        );
        queueSummary.textContent = `${queue.length} PDF(s) across ${folders.size} folder(s) • ${formatBytes(totalBytes)}`;

        const badgeLabels = {
            queued: "Queued",
            uploading: "Uploading...",
            uploaded: "✅ Uploaded",
            skipped: "⏭️ Duplicate",
            failed: "❌ Failed",
        };

        // Render rows efficiently
        const rowsHtml = queue
            .map((item) => {
                const detailHtml = item.detail
                    ? `<span class="file-detail">${escapeHtml(item.detail)}</span>`
                    : "";
                const removeDisabled = isUploading ? "disabled" : "";
                return `<tr data-id="${item.id}">
                    <td class="file-path-cell">${escapeHtml(item.relativePath)}${detailHtml}</td>
                    <td>${formatBytes(item.file.size)}</td>
                    <td><span class="badge ${item.status}">${badgeLabels[item.status] || item.status}</span></td>
                    <td><button class="remove-btn" data-remove-id="${item.id}" title="Remove from queue" ${removeDisabled}>✕</button></td>
                </tr>`;
            })
            .join("");

        queueTbody.innerHTML = rowsHtml;
    }

    function updateRowUI(item) {
        const row = queueTbody.querySelector(`tr[data-id="${item.id}"]`);
        if (!row) return;
        const badgeLabels = {
            queued: "Queued",
            uploading: "Uploading...",
            uploaded: "✅ Uploaded",
            skipped: "⏭️ Duplicate",
            failed: "❌ Failed",
        };
        const pathCell = row.children[0];
        const detailHtml = item.detail ? `<span class="file-detail">${escapeHtml(item.detail)}</span>` : "";
        pathCell.innerHTML = `${escapeHtml(item.relativePath)}${detailHtml}`;
        row.children[2].innerHTML = `<span class="badge ${item.status}">${badgeLabels[item.status] || item.status}</span>`;
    }

    function updateProgressUI() {
        const total = queue.length;
        const uploaded = queue.filter((i) => i.status === "uploaded").length;
        const skipped = queue.filter((i) => i.status === "skipped").length;
        const failed = queue.filter((i) => i.status === "failed").length;
        const remaining = queue.filter((i) => i.status === "queued" || i.status === "uploading").length;
        const done = uploaded + skipped + failed;
        const pct = total > 0 ? Math.round((done / total) * 100) : 0;

        statUploaded.textContent = uploaded;
        statSkipped.textContent = skipped;
        statFailed.textContent = failed;
        statRemaining.textContent = remaining;

        progressFill.style.width = `${pct}%`;
        progressFill.textContent = `${pct}%`;

        const hasQueued = queue.some((i) => i.status === "queued");
        startUploadBtn.disabled = isUploading || !hasQueued;
        stopUploadBtn.disabled = !isUploading;
        clearQueueBtn.disabled = isUploading || total === 0;

        if (!isUploading && failed > 0) {
            retryFailedBtn.classList.remove("hidden");
            retryFailedBtn.textContent = `🔁 Retry Failed (${failed})`;
        } else {
            retryFailedBtn.classList.add("hidden");
        }
    }

    // --- Drag & Drop Recursive Directory Scanner ---
    async function readEntryRecursively(entry, prefix = "") {
        if (entry.isFile) {
            return new Promise((resolve) => {
                entry.file(
                    (file) => resolve([{ file, relativePath: prefix ? `${prefix}/${file.name}` : file.name }]),
                    () => resolve([])
                );
            });
        }
        if (entry.isDirectory) {
            const dirReader = entry.createReader();
            const entries = [];
            const readAllBatch = () =>
                new Promise((resolve) => {
                    dirReader.readEntries((batch) => {
                        if (!batch.length) {
                            resolve();
                        } else {
                            entries.push(...batch);
                            readAllBatch().then(resolve);
                        }
                    }, () => resolve());
                });
            await readAllBatch();
            const nextPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
            const nested = [];
            for (const child of entries) {
                const childFiles = await readEntryRecursively(child, nextPrefix);
                nested.push(...childFiles);
            }
            return nested;
        }
        return [];
    }

    // --- Upload Loop ---
    async function runUploadLoop() {
        const pending = queue.filter((i) => i.status === "queued");
        if (pending.length === 0) return;

        isUploading = true;
        shouldStop = false;
        hideBanner(uploadStatus);
        renderQueue();
        updateProgressUI();

        appendLog("INFO", `Starting upload batch of ${pending.length} PDF(s)...`);

        for (const item of pending) {
            if (shouldStop) {
                appendLog("WARNING", "Upload stopped by user.");
                showBanner(uploadStatus, "Upload paused/stopped by user.", "warning");
                break;
            }

            if (item.file.size > maxUploadMb * 1024 * 1024) {
                item.status = "failed";
                item.detail = `File exceeds ${maxUploadMb} MB limit (${formatBytes(item.file.size)})`;
                updateRowUI(item);
                updateProgressUI();
                appendLog("ERROR", `${item.relativePath}: ${item.detail}`);
                continue;
            }

            item.status = "uploading";
            item.detail = "Uploading to Mendeley...";
            currentFileEl.textContent = item.relativePath;
            updateRowUI(item);
            updateProgressUI();

            const formData = new FormData();
            formData.append("file", item.file, item.file.name);
            formData.append("relative_path", item.relativePath);
            formData.append("check_manifest", dedupManifestCheck.checked ? "true" : "false");
            formData.append("check_catalog", dedupCatalogCheck.checked ? "true" : "false");
            formData.append("doc_type", docTypeSelect.value);

            activeAbortController = new AbortController();
            try {
                const resp = await fetch("api/upload-file", {
                    method: "POST",
                    body: formData,
                    signal: activeAbortController.signal,
                });

                if (resp.status === 401) {
                    appendLog("ERROR", "Session expired. Please sign in again.");
                    window.location.reload();
                    return;
                }

                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    throw new Error(data.detail || `HTTP ${resp.status}`);
                }

                if (data.status === "uploaded") {
                    item.status = "uploaded";
                    item.docId = data.document_id;
                    item.detail = `Doc ID: ${data.document_id} (${data.duration_s}s)${data.hash_verified ? " • SHA-1 verified" : ""}`;
                    appendLog("SUCCESS", `${item.relativePath} → Uploaded (doc_id=${data.document_id}, ${data.duration_s}s)`);
                } else if (data.status === "skipped") {
                    item.status = "skipped";
                    item.docId = data.document_id;
                    item.detail = `${data.message} (Doc ID: ${data.document_id})`;
                    appendLog("SKIP", `${item.relativePath} → ${data.message}`);
                } else {
                    throw new Error(data.message || "Unexpected server response");
                }

                if (typeof data.manifest_count === "number") {
                    connectionSummary.textContent = `Mendeley credentials configured • ${data.manifest_count.toLocaleString()} books tracked in manifest ledger • Max ${maxUploadMb} MB/PDF`;
                }
            } catch (err) {
                if (err.name === "AbortError") {
                    item.status = "queued";
                    item.detail = "Cancelled";
                    appendLog("WARNING", `Cancelled in-flight upload: ${item.relativePath}`);
                } else {
                    item.status = "failed";
                    item.detail = err.message || "Upload failed";
                    appendLog("ERROR", `${item.relativePath} → ${item.detail}`);
                }
            } finally {
                activeAbortController = null;
                updateRowUI(item);
                updateProgressUI();
            }
        }

        isUploading = false;
        currentFileEl.textContent = "—";
        renderQueue();
        updateProgressUI();

        if (!shouldStop) {
            const uploaded = queue.filter((i) => i.status === "uploaded").length;
            const skipped = queue.filter((i) => i.status === "skipped").length;
            const failed = queue.filter((i) => i.status === "failed").length;
            if (failed === 0) {
                showBanner(uploadStatus, `✅ Batch complete! Uploaded: ${uploaded}, Duplicates skipped: ${skipped}.`, "success");
            } else {
                showBanner(uploadStatus, `Batch finished with ${failed} failure(s). Uploaded: ${uploaded}, Skipped: ${skipped}. Click "Retry Failed" to retry.`, "warning");
            }
            appendLog("INFO", `Batch finished — Uploaded: ${uploaded}, Skipped: ${skipped}, Failed: ${failed}.`);
        }
    }

    // --- Event Listeners ---
    checkConnectionBtn.addEventListener("click", () => loadServerInfo(true));

    browseFolderBtn.addEventListener("click", () => {
        folderInput.value = "";
        folderInput.click();
    });

    browseFilesBtn.addEventListener("click", () => {
        filesInput.value = "";
        filesInput.click();
    });

    folderInput.addEventListener("change", (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        addFilesToQueue(files.map((f) => ({ file: f, relativePath: f.webkitRelativePath || f.name })));
    });

    filesInput.addEventListener("change", (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        addFilesToQueue(files.map((f) => ({ file: f, relativePath: f.name })));
    });

    // Drag & Drop handlers
    ["dragenter", "dragover"].forEach((evtName) => {
        dropzone.addEventListener(evtName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.add("drag-over");
        });
    });

    ["dragleave", "drop"].forEach((evtName) => {
        dropzone.addEventListener(evtName, (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropzone.classList.remove("drag-over");
        });
    });

    dropzone.addEventListener("drop", async (e) => {
        const items = Array.from(e.dataTransfer.items || []);
        if (items.length && typeof items[0].webkitGetAsEntry === "function") {
            showBanner(selectionStatus, "Scanning dropped folder(s)...", "info");
            const collected = [];
            for (const item of items) {
                const entry = item.webkitGetAsEntry();
                if (entry) {
                    const files = await readEntryRecursively(entry);
                    collected.push(...files);
                }
            }
            addFilesToQueue(collected);
        } else {
            const files = Array.from(e.dataTransfer.files || []);
            addFilesToQueue(files.map((f) => ({ file: f, relativePath: f.name })));
        }
    });

    queueTbody.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-remove-id]");
        if (!btn || isUploading) return;
        const id = Number(btn.getAttribute("data-remove-id"));
        queue = queue.filter((item) => item.id !== id);
        renderQueue();
        updateProgressUI();
    });

    startUploadBtn.addEventListener("click", () => runUploadLoop());

    stopUploadBtn.addEventListener("click", () => {
        shouldStop = true;
        stopUploadBtn.disabled = true;
        if (activeAbortController) {
            activeAbortController.abort();
        }
    });

    retryFailedBtn.addEventListener("click", () => {
        queue.forEach((item) => {
            if (item.status === "failed") {
                item.status = "queued";
                item.detail = "Retrying...";
            }
        });
        renderQueue();
        updateProgressUI();
        runUploadLoop();
    });

    clearQueueBtn.addEventListener("click", () => {
        if (isUploading) return;
        queue = [];
        hideBanner(selectionStatus);
        hideBanner(uploadStatus);
        renderQueue();
        updateProgressUI();
    });

    clearLogBtn.addEventListener("click", () => {
        logWindow.innerHTML = '<div class="log-entry log-INFO">Log cleared.</div>';
    });

    // Init
    loadServerInfo(false);
})();
