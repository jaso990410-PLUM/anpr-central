(() => {
    const dialog = document.querySelector("[data-folder-picker]");
    const openButton = document.querySelector("[data-open-folder-picker]");
    const target = document.querySelector("#storage-root");
    if (!dialog || !openButton || !target) return;

    const currentLabel = dialog.querySelector("[data-folder-current]");
    const list = dialog.querySelector("[data-folder-list]");
    const upButton = dialog.querySelector("[data-folder-up]");
    const createButton = dialog.querySelector("[data-folder-create]");
    const selectButton = dialog.querySelector("[data-folder-select]");
    const cancelButton = dialog.querySelector("[data-folder-cancel]");
    const error = dialog.querySelector("[data-folder-error]");
    let currentPath = null;
    let parentPath = null;

    async function load(path = "") {
        error.hidden = true;
        list.replaceChildren();
        const loading = document.createElement("span");
        loading.textContent = "Loading folders…";
        list.append(loading);
        try {
            const response = await fetch(`/media/storage/folders?path=${encodeURIComponent(path)}`, {
                headers: {Accept: "application/json"},
                credentials: "same-origin",
            });
            if (!response.ok) throw new Error("This folder cannot be accessed by ANPR Central.");
            const payload = await response.json();
            currentPath = payload.current;
            parentPath = payload.parent;
            currentLabel.textContent = currentPath || "Select a drive";
            upButton.disabled = !parentPath;
            selectButton.disabled = !currentPath;
            createButton.disabled = !currentPath;
            list.replaceChildren();
            payload.directories.forEach((folder) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "button-quiet";
                button.textContent = folder.name;
                button.addEventListener("click", () => load(folder.path));
                list.append(button);
            });
            if (!payload.directories.length) {
                const empty = document.createElement("span");
                empty.textContent = "No subfolders. You may select this folder.";
                list.append(empty);
            }
        } catch (reason) {
            list.replaceChildren();
            error.textContent = reason.message;
            error.hidden = false;
        }
    }

    openButton.addEventListener("click", () => {
        dialog.showModal();
        load(target.value || "");
    });
    upButton.addEventListener("click", () => parentPath && load(parentPath));
    createButton.addEventListener("click", async () => {
        if (!currentPath) return;
        const name = window.prompt("New folder name");
        if (!name || !name.trim()) return;
        error.hidden = true;
        const csrf = document.querySelector('form[action="/media/storage"] input[name="csrf_token"]')?.value;
        const body = new URLSearchParams({parent_path: currentPath, folder_name: name.trim(), csrf_token: csrf || ""});
        try {
            const response = await fetch("/media/storage/folders", {
                method: "POST",
                headers: {"Content-Type": "application/x-www-form-urlencoded", Accept: "application/json"},
                credentials: "same-origin",
                body,
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "The folder could not be created.");
            await load(payload.path);
        } catch (reason) {
            error.textContent = reason.message;
            error.hidden = false;
        }
    });
    cancelButton.addEventListener("click", () => dialog.close());
    selectButton.addEventListener("click", () => {
        if (!currentPath) return;
        target.value = currentPath;
        dialog.close();
    });
})();
