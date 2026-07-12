async function init() {
    const bridge = window.AstrBotPluginPage;

    // Wait for the bridge SDK to connect
    try {
        await bridge.ready();
    } catch (e) {
        console.error("Failed to connect to AstrBot Bridge:", e);
    }

    // State Variables
    let allMemos = {};
    let allTasks = {};
    let allTriggers = {};

    // Tab switching logic (Main Navigation)
    const tabButtons = document.querySelectorAll(".tab-btn");
    const tabViews = document.querySelectorAll(".tab-view");
    
    tabButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");
            
            tabButtons.forEach(b => b.classList.remove("active"));
            tabViews.forEach(v => v.classList.remove("active"));
            
            btn.classList.add("active");
            document.getElementById(`view-${targetTab}`).classList.add("active");
            
            if (typeof lucide !== "undefined") {
                lucide.createIcons();
            }
        });
    });

    // Settings sub-tab switching logic (Sidebar Navigation)
    const subTabButtons = document.querySelectorAll(".sub-tab-btn");
    const subTabViews = document.querySelectorAll(".sub-tab-content");

    subTabButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetSubTab = btn.getAttribute("data-sub-tab");

            subTabButtons.forEach(b => b.classList.remove("active"));
            subTabViews.forEach(v => v.classList.remove("active"));

            btn.classList.add("active");
            document.getElementById(`sub-tab-content-${targetSubTab}`).classList.add("active");

            if (typeof lucide !== "undefined") {
                lucide.createIcons();
            }
        });
    });

    // Hide inner back button if embedded in iframe (parent header already has a back button)
    const backBtn = document.getElementById("back-btn");
    if (window.self !== window.top) {
        backBtn.style.display = "none";
    } else {
        backBtn.addEventListener("click", () => {
            showToast("请使用外部导航返回");
        });
    }

    // Custom Select Dropdowns state management
    setupCustomSelects();

    function setupCustomSelects() {
        const selects = document.querySelectorAll(".custom-select");
        
        selects.forEach(select => {
            const trigger = select.querySelector(".custom-select-trigger");
            const options = select.querySelectorAll(".custom-option");
            
            trigger.addEventListener("click", (e) => {
                e.stopPropagation();
                selects.forEach(s => {
                    if (s !== select) s.classList.remove("open");
                });
                select.classList.toggle("open");
            });

            options.forEach(opt => {
                opt.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const val = opt.getAttribute("data-value");
                    const label = opt.textContent;
                    
                    select.setAttribute("data-value", val);
                    select.querySelector(".custom-select-value").textContent = label;
                    
                    options.forEach(o => o.classList.remove("selected"));
                    opt.classList.add("selected");
                    
                    select.classList.remove("open");

                    // Custom trigger event for task type changes
                    if (select.id === "select-task-type") {
                        handleTaskTypeChange(val);
                    }
                });
            });
        });

        document.addEventListener("click", () => {
            selects.forEach(s => s.classList.remove("open"));
        });
    }

    function setCustomSelectValue(selectId, val) {
        const select = document.getElementById(selectId);
        if (!select) return;
        
        const option = select.querySelector(`.custom-option[data-value="${val}"]`);
        if (option) {
            select.setAttribute("data-value", val);
            select.querySelector(".custom-select-value").textContent = option.textContent;
            
            select.querySelectorAll(".custom-option").forEach(o => o.classList.remove("selected"));
            option.classList.add("selected");
        }
    }

    function getCustomSelectValue(selectId) {
        const select = document.getElementById(selectId);
        return select ? select.getAttribute("data-value") : "";
    }

    // Toast alerts helper
    function showToast(message, isError = false) {
        const toast = document.getElementById("toast");
        toast.textContent = message;
        toast.style.backgroundColor = isError ? "var(--color-danger)" : "var(--text-primary)";
        toast.style.color = isError ? "#ffffff" : "var(--bg-card)";
        toast.classList.add("show");
        
        setTimeout(() => {
            toast.classList.remove("show");
        }, 3000);
    }

    // Date/Time helper functions
    function formatTime(timestamp) {
        if (!timestamp) return "-";
        const date = new Date(timestamp * 1000);
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
    }

    // Dynamic remaining time calculation
    function getRemainingTime(timestamp) {
        const diffMs = timestamp * 1000 - Date.now();
        if (diffMs <= 0) return "已过期";
        const diffMins = Math.round(diffMs / 60000);
        if (diffMins < 60) return `${diffMins} 分钟后`;
        const diffHours = Math.floor(diffMins / 60);
        const remMins = diffMins % 60;
        if (diffHours < 24) return `${diffHours} 小时 ${remMins} 分钟后`;
        const diffDays = Math.floor(diffHours / 24);
        return `${diffDays} 天后`;
    }

    // Handle scheduled task type form label updates
    function handleTaskTypeChange(type) {
        const valueLabel = document.getElementById("task-value-label");
        const valueHint = document.getElementById("task-value-hint");
        const valueInput = document.getElementById("task-value");

        if (type === "one_off") {
            valueLabel.textContent = "触发时间参数 (分钟后)";
            valueHint.textContent = "请输入多少分钟后执行本提醒（输入一个正整数，如：30）";
            valueInput.placeholder = "例如 30";
        } else if (type === "daily") {
            valueLabel.textContent = "每日触发时刻 (HH:MM)";
            valueHint.textContent = "请输入每日定点执行时刻（输入 HH:MM 格式，如：08:30 或 22:00）";
            valueInput.placeholder = "例如 08:30";
        } else if (type === "interval") {
            valueLabel.textContent = "循环提醒间隔 (分钟)";
            valueHint.textContent = "请输入循环间隔时长（输入分钟数，如：60 表示每隔 1 小时提醒一次）";
            valueInput.placeholder = "例如 60";
        }
    }

    // Modal Control logic
    const modal = document.getElementById("item-modal");
    const itemForm = document.getElementById("item-form");
    
    // Close Modal actions (bound to the span wrapper `#modal-close-btn` which survives Lucide rendering)
    document.getElementById("modal-close-btn").addEventListener("click", closeModal);
    document.getElementById("modal-cancel-btn").addEventListener("click", closeModal);
    
    modal.addEventListener("click", (e) => {
        if (e.target === modal) {
            closeModal();
        }
    });

    function closeModal() {
        modal.classList.remove("active");
        itemForm.reset();
    }

    // Confirm Delete Modal control
    let currentDeleteType = null;
    let currentDeleteId = null;
    const confirmModal = document.getElementById("confirm-modal-overlay");
    
    document.getElementById("confirm-modal-cancel-btn").addEventListener("click", closeConfirmModal);
    confirmModal.addEventListener("click", (e) => {
        if (e.target === confirmModal) {
            closeConfirmModal();
        }
    });

    function closeConfirmModal() {
        confirmModal.classList.remove("active");
        currentDeleteType = null;
        currentDeleteId = null;
    }

    document.getElementById("confirm-modal-ok-btn").addEventListener("click", async () => {
        if (!currentDeleteType || !currentDeleteId) return;
        const okBtn = document.getElementById("confirm-modal-ok-btn");
        okBtn.disabled = true;
        okBtn.textContent = "删除中...";
        try {
            const res = await bridge.apiPost("delete_item", { type: currentDeleteType, id: currentDeleteId });
            if (res && res.status === "success") {
                showToast("删除成功！");
                closeConfirmModal();
                await loadData();
            } else {
                showToast("删除失败: " + (res.message || "未知错误"), true);
            }
        } catch (err) {
            showToast("请求失败: " + err.message, true);
        } finally {
            okBtn.disabled = false;
            okBtn.textContent = "确定";
        }
    });

    function openModal(type, action = "add", id = null) {
        document.getElementById("item-type").value = type;
        document.getElementById("item-action").value = action;
        document.getElementById("item-id").value = id || "";

        // Set modal title
        const typeCN = {
            "status_memo": "人设备忘录",
            "task": "定时/周期提醒",
            "keyword_trigger": "关键词监听"
        };
        const actionCN = action === "add" ? "新增" : "编辑";
        document.getElementById("modal-title-text").textContent = `${actionCN}${typeCN[type]}`;

        // Toggle form fields
        document.querySelectorAll(".form-fields-group").forEach(el => {
            el.style.display = "none";
        });
        document.getElementById(`fields-${type}`).style.display = "block";

        // Reset or populate field values
        if (action === "add") {
            itemForm.reset();
            if (type === "status_memo") {
                document.getElementById("status-minutes").value = 60;
                document.getElementById("status-umo").value = "GLOBAL";
            } else if (type === "task") {
                setCustomSelectValue("select-task-type", "one_off");
                handleTaskTypeChange("one_off");
                document.getElementById("task-context").value = 5;
                document.getElementById("task-umo").value = "GLOBAL";
            } else if (type === "keyword_trigger") {
                document.getElementById("trigger-context").value = 5;
                document.getElementById("trigger-umo").value = "GLOBAL";
            }
        } else {
            // Edit mode: Populate form fields from cache
            if (type === "status_memo") {
                const item = allMemos[id];
                document.getElementById("status-content").value = item.content;
                
                // Calculate remaining minutes for expire field
                const remainingMins = Math.max(1, Math.round((item.expire_timestamp - Date.now()/1000) / 60));
                document.getElementById("status-minutes").value = remainingMins;
                document.getElementById("status-umo").value = item.target_umo || "GLOBAL";
            } else if (type === "task") {
                const item = allTasks[id];
                document.getElementById("task-desc").value = item.task_description;
                setCustomSelectValue("select-task-type", item.type);
                handleTaskTypeChange(item.type);
                document.getElementById("task-value").value = item.scheduled_time;
                document.getElementById("task-umo").value = item.target_umo;
                document.getElementById("task-context").value = item.context_history_limit || 5;
            } else if (type === "keyword_trigger") {
                const item = allTriggers[id];
                document.getElementById("trigger-keyword").value = item.keyword;
                document.getElementById("trigger-desc").value = item.task_description;
                document.getElementById("trigger-context").value = item.context_history_limit || 5;
                document.getElementById("trigger-umo").value = item.target_umo || "GLOBAL";
            }
        }

        modal.classList.add("active");
        if (typeof lucide !== "undefined") {
            lucide.createIcons();
        }
    }

    // Modal Add Button Click Listeners
    document.getElementById("add-status-btn").addEventListener("click", () => openModal("status_memo", "add"));
    document.getElementById("add-task-btn").addEventListener("click", () => openModal("task", "add"));
    document.getElementById("add-trigger-btn").addEventListener("click", () => openModal("keyword_trigger", "add"));

    // Item Form Submit Handler (Saves Add or Edit actions)
    itemForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        
        const type = document.getElementById("item-type").value;
        const action = document.getElementById("item-action").value;
        const id = document.getElementById("item-id").value;

        let payload = {};
        
        if (type === "status_memo") {
            payload = {
                content: document.getElementById("status-content").value,
                minutes_later: parseInt(document.getElementById("status-minutes").value) || 60,
                target_umo: document.getElementById("status-umo").value.trim()
            };
        } else if (type === "task") {
            payload = {
                task_description: document.getElementById("task-desc").value,
                task_type: getCustomSelectValue("select-task-type"),
                schedule_value: document.getElementById("task-value").value.trim(),
                target_umo: document.getElementById("task-umo").value.trim(),
                context_history_limit: parseInt(document.getElementById("task-context").value) || 5
            };
        } else if (type === "keyword_trigger") {
            payload = {
                keyword: document.getElementById("trigger-keyword").value.trim(),
                task_description: document.getElementById("trigger-desc").value,
                context_history_limit: parseInt(document.getElementById("trigger-context").value) || 5,
                target_umo: document.getElementById("trigger-umo").value.trim()
            };
        }
        
        // JS Validation for the active form fields
        if (type === "status_memo") {
            if (!payload.content || !payload.content.trim()) {
                showToast("请输入备忘录内容", true);
                return;
            }
            if (isNaN(payload.minutes_later) || payload.minutes_later <= 0) {
                showToast("请输入有效的正整数时长", true);
                return;
            }
            if (!payload.target_umo) {
                showToast("请输入作用的群号/会话 ID", true);
                return;
            }
        } else if (type === "task") {
            if (!payload.task_description || !payload.task_description.trim()) {
                showToast("请输入任务计划描述", true);
                return;
            }
            if (!payload.schedule_value) {
                showToast("请输入触发时间参数", true);
                return;
            }
            if (!payload.target_umo) {
                showToast("请输入推送目标会话 ID", true);
                return;
            }
        } else if (type === "keyword_trigger") {
            if (!payload.keyword) {
                showToast("请输入触发关键词", true);
                return;
            }
            if (!payload.task_description || !payload.task_description.trim()) {
                showToast("请输入 AI 回复/搭话策略描述", true);
                return;
            }
            if (!payload.target_umo) {
                showToast("请输入作用的群号/会话 ID", true);
                return;
            }
        }

        const submitBtn = document.getElementById("modal-submit-btn");
        submitBtn.disabled = true;
        submitBtn.textContent = "保存中...";

        try {
            let res;
            if (action === "add") {
                res = await bridge.apiPost("add_item", { type, ...payload });
            } else {
                res = await bridge.apiPost("update_item", { type, id, data: payload });
            }

            if (res && res.status === "success") {
                showToast("保存成功！");
                closeModal();
                await loadData();
            } else {
                showToast("保存失败: " + (res.message || "未知错误"), true);
            }
        } catch (err) {
            showToast("操作失败: " + err.message, true);
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = "确认";
        }
    });

    // Global Save Config Settings handler
    const saveConfigBtn = document.getElementById("save-config-btn");
    const configForm = document.getElementById("config-form");
    
    configForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        saveConfigBtn.disabled = true;
        saveConfigBtn.textContent = "保存中...";

        const payload = {
            poll_interval: parseInt(document.getElementById("config-poll-interval").value) || 15,
            trigger_mode: getCustomSelectValue("select-trigger-mode"),
            context_history_limit: parseInt(document.getElementById("config-context-limit").value) || 5,
            ai_allow_add: document.getElementById("config-ai-allow-add").checked,
            ai_allow_update: document.getElementById("config-ai-allow-update").checked,
            ai_allow_delete: document.getElementById("config-ai-allow-delete").checked,
            enable_status_memo_ai: document.getElementById("config-enable-status-memo-ai").checked,
            enable_task_ai: document.getElementById("config-enable-task-ai").checked,
            enable_keyword_trigger_ai: document.getElementById("config-enable-keyword-trigger-ai").checked,
            allow_global_memo: document.getElementById("config-allow-global-memo").checked,
            group_filter_mode: getCustomSelectValue("select-group-filter-mode"),
            group_list: document.getElementById("config-group-list").value.trim()
        };

        try {
            const res = await bridge.apiPost("save_config", payload);
            if (res && res.status === "success") {
                showToast("插件设置保存成功！已实时生效。");
            } else {
                showToast("保存失败: " + (res.message || "未知错误"), true);
            }
        } catch (err) {
            showToast("保存失败: " + err.message, true);
        } finally {
            saveConfigBtn.disabled = false;
            saveConfigBtn.innerHTML = `<i data-lucide="save" class="btn-icon"></i><span>保存配置</span>`;
            if (typeof lucide !== "undefined") {
                lucide.createIcons();
            }
        }
    });

    // Delete item handler
    function deleteItem(type, id) {
        const labelText = {
            "status_memo": "这条状态备忘录",
            "task": "这条定时提醒计划",
            "keyword_trigger": "这个关键词搭话监听"
        };

        currentDeleteType = type;
        currentDeleteId = id;
        document.getElementById("confirm-modal-text").textContent = `确定要删除 ${labelText[type]} 吗？`;
        confirmModal.classList.add("active");
    }

    // Load memos list and configuration options from backend Quart APIs
    async function loadData() {
        try {
            const res = await bridge.apiGet("get_data", {});
            if (res && res.status === "success") {
                // Save locally for editing forms
                allMemos = res.status_memos || {};
                allTasks = res.tasks || {};
                allTriggers = res.keyword_triggers || {};

                // 1. Render Status Memos
                const statusBody = document.getElementById("status-memos-body");
                const memoKeys = Object.keys(allMemos);
                if (memoKeys.length === 0) {
                    statusBody.innerHTML = `
                        <tr>
                            <td colspan="4" class="table-empty">暂无状态备忘录，AI 可以在聊天中自主设定临时状态或在上方手动添加。</td>
                        </tr>
                    `;
                } else {
                    statusBody.innerHTML = memoKeys.map(key => {
                        const memo = allMemos[key];
                        const scope = memo.target_umo === "GLOBAL" ? "全局" : `当前会话 (${memo.target_umo})`;
                        const isExpired = Date.now() / 1000 > memo.expire_timestamp;
                        const remainingStr = isExpired ? "已失效" : getRemainingTime(memo.expire_timestamp);
                        
                        return `
                            <tr>
                                <td class="cell-wrap">${escapeHtml(memo.content)}</td>
                                <td>${escapeHtml(scope)}</td>
                                <td><span class="status-badge ${isExpired ? 'failed' : 'active-status'}">${remainingStr}</span></td>
                                <td>
                                    <div class="actions-cell">
                                        <button class="btn btn-edit edit-memo-btn" data-id="${key}">编辑</button>
                                        <button class="btn btn-danger delete-memo-btn" data-id="${key}">删除</button>
                                    </div>
                                </td>
                            </tr>
                        `;
                    }).join("");
                }

                // 2. Render Tasks
                const tasksBody = document.getElementById("tasks-body");
                const taskKeys = Object.keys(allTasks);
                if (taskKeys.length === 0) {
                    tasksBody.innerHTML = `
                        <tr>
                            <td colspan="6" class="table-empty">暂无定时任务，AI 可以在聊天中自主添加提醒任务或在上方手动添加。</td>
                        </tr>
                    `;
                } else {
                    tasksBody.innerHTML = taskKeys.map(key => {
                        const task = allTasks[key];
                        const typeCN = { "one_off": "单次", "daily": "每日", "interval": "周期性间隔" };
                        const statusCN = { "pending": "等待生成", "generating": "正在生成", "ready": "生成就绪", "failed": "生成失败" };
                        
                        let schedValueDisplay = task.scheduled_time;
                        if (task.type === "one_off" || task.type === "interval") {
                            schedValueDisplay += " 分钟";
                        }
                        
                        return `
                            <tr>
                                <td class="cell-wrap">${escapeHtml(task.task_description)}</td>
                                <td><span class="type-indicator">${typeCN[task.type] || task.type}</span></td>
                                <td>${schedValueDisplay}</td>
                                <td><code style="font-family: monospace;">${escapeHtml(task.target_umo)}</code></td>
                                <td><span class="status-badge ${task.status}">${statusCN[task.status] || task.status}</span></td>
                                <td>
                                    <div class="actions-cell">
                                        <button class="btn btn-edit edit-task-btn" data-id="${key}">编辑</button>
                                        <button class="btn btn-danger delete-task-btn" data-id="${key}">删除</button>
                                    </div>
                                </td>
                            </tr>
                        `;
                    }).join("");
                }

                // 3. Render Triggers
                const triggersBody = document.getElementById("triggers-body");
                const triggerKeys = Object.keys(allTriggers);
                if (triggerKeys.length === 0) {
                    triggersBody.innerHTML = `
                        <tr>
                            <td colspan="5" class="table-empty">暂无关键词监听器，AI 可以在聊天中自主注册或在上方手动添加。</td>
                        </tr>
                    `;
                } else {
                    triggersBody.innerHTML = triggerKeys.map(key => {
                        const trigger = allTriggers[key];
                        const scope = trigger.target_umo === "GLOBAL" ? "全局" : `当前会话 (${trigger.target_umo})`;
                        
                        return `
                            <tr>
                                <td><strong style="color: var(--color-primary); font-size: 15px;">"${escapeHtml(trigger.keyword)}"</strong></td>
                                <td class="cell-wrap">${escapeHtml(trigger.task_description)}</td>
                                <td>${escapeHtml(scope)}</td>
                                <td>${trigger.context_history_limit || 5} 条</td>
                                <td>
                                    <div class="actions-cell">
                                        <button class="btn btn-edit edit-trigger-btn" data-id="${key}">编辑</button>
                                        <button class="btn btn-danger delete-trigger-btn" data-id="${key}">删除</button>
                                    </div>
                                </td>
                            </tr>
                        `;
                    }).join("");
                }

                // Attach dynamic button action listeners
                attachActionListeners();

                // 4. Populate configuration settings
                const config = res.config || {};
                document.getElementById("config-poll-interval").value = config.poll_interval || 15;
                setCustomSelectValue("select-trigger-mode", config.trigger_mode || "tool");
                document.getElementById("config-context-limit").value = config.context_history_limit || 5;

                document.getElementById("config-ai-allow-add").checked = config.ai_allow_add !== false;
                document.getElementById("config-ai-allow-update").checked = config.ai_allow_update !== false;
                document.getElementById("config-ai-allow-delete").checked = config.ai_allow_delete !== false;

                document.getElementById("config-enable-status-memo-ai").checked = config.enable_status_memo_ai !== false;
                document.getElementById("config-enable-task-ai").checked = config.enable_task_ai !== false;
                document.getElementById("config-enable-keyword-trigger-ai").checked = config.enable_keyword_trigger_ai !== false;

                document.getElementById("config-allow-global-memo").checked = config.allow_global_memo !== false;
                setCustomSelectValue("select-group-filter-mode", config.group_filter_mode || "all");
                document.getElementById("config-group-list").value = config.group_list || "";

                if (typeof lucide !== "undefined") {
                    lucide.createIcons();
                }
            }
        } catch (e) {
            console.error("Failed to load memo data & config:", e);
            showToast("获取备忘录条目与配置失败: " + e.message, true);
        }
    }

    function attachActionListeners() {
        // Edit status memo
        document.querySelectorAll(".edit-memo-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-id");
                openModal("status_memo", "edit", id);
            });
        });

        // Delete status memo
        document.querySelectorAll(".delete-memo-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-id");
                deleteItem("status_memo", id);
            });
        });

        // Edit scheduled task
        document.querySelectorAll(".edit-task-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-id");
                openModal("task", "edit", id);
            });
        });

        // Delete scheduled task
        document.querySelectorAll(".delete-task-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-id");
                deleteItem("task", id);
            });
        });

        // Edit keyword trigger
        document.querySelectorAll(".edit-trigger-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-id");
                openModal("keyword_trigger", "edit", id);
            });
        });

        // Delete keyword trigger
        document.querySelectorAll(".delete-trigger-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-id");
                deleteItem("keyword_trigger", id);
            });
        });
    }

    // Helper to escape HTML tags to prevent XSS
    function escapeHtml(str) {
        if (!str) return "";
        return str
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    // Initial load
    await loadData();
}

// Robust iframe execution check (fires immediately if readyState is already loaded)
if (document.readyState === "complete" || document.readyState === "interactive") {
    init();
} else {
    document.addEventListener("DOMContentLoaded", init);
}
