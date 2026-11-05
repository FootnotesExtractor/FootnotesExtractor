// Main JavaScript utilities for FootnotesExtractor Web App

class PDFExtractorApp {
    constructor() {
        this.currentJob = null;
        this.statusPollingInterval = null;
        this.maxFileSize = 200 * 1024 * 1024; // 200MB, app.py's MAX_PDF_MB
        this.allowedTypes = ['application/pdf'];
        
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.initializeTooltips();
        this.setupDragAndDrop();
    }

    setupEventListeners() {
        // File input change
        const fileInput = document.getElementById('pdfFile');
        if (fileInput) {
            fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }

        // Form submission
        const uploadForm = document.getElementById('uploadForm');
        if (uploadForm) {
            uploadForm.addEventListener('submit', (e) => this.handleFormSubmit(e));
        }

        // Range sliders
        const sliders = document.querySelectorAll('.form-range');
        sliders.forEach(slider => {
            slider.addEventListener('input', (e) => this.updateSliderValue(e));
        });

        // Modal events
        const modals = document.querySelectorAll('.modal');
        modals.forEach(modal => {
            modal.addEventListener('show.bs.modal', () => this.pauseStatusPolling());
            modal.addEventListener('hide.bs.modal', () => this.resumeStatusPolling());
        });
    }

    initializeTooltips() {
        const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
        tooltipTriggerList.map(function (tooltipTriggerEl) {
            return new bootstrap.Tooltip(tooltipTriggerEl);
        });
    }

    setupDragAndDrop() {
        const uploadArea = document.getElementById('uploadArea');
        if (!uploadArea) return;

        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            uploadArea.addEventListener(eventName, this.preventDefaults, false);
            document.body.addEventListener(eventName, this.preventDefaults, false);
        });

        ['dragenter', 'dragover'].forEach(eventName => {
            uploadArea.addEventListener(eventName, () => this.highlight(uploadArea), false);
        });

        ['dragleave', 'drop'].forEach(eventName => {
            uploadArea.addEventListener(eventName, () => this.unhighlight(uploadArea), false);
        });

        uploadArea.addEventListener('drop', (e) => this.handleDrop(e), false);
    }

    preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    highlight(element) {
        element.classList.add('dragover');
    }

    unhighlight(element) {
        element.classList.remove('dragover');
    }

    handleDrop(e) {
        const dt = e.dataTransfer;
        const files = dt.files;

        this.handleFiles(files);
    }

    handleFiles(files) {
        if (files.length > 0) {
            const file = files[0];
            if (this.validateFile(file)) {
                const fileInput = document.getElementById('pdfFile');
                if (fileInput) {
                    fileInput.files = files;
                    this.updateFileDisplay(file);
                }
            }
        }
    }

    handleFileSelect(e) {
        const files = e.target.files;
        if (files.length > 0) {
            const file = files[0];
            if (this.validateFile(file)) {
                this.updateFileDisplay(file);
            } else {
                e.target.value = ''; // Clear invalid file
            }
        }
    }

    validateFile(file) {
        // Check file type
        if (!this.allowedTypes.includes(file.type)) {
            this.showError('Please select a PDF file.');
            return false;
        }

        // Check file size
        if (file.size > this.maxFileSize) {
            this.showError(`File size exceeds ${this.maxFileSize / (1024 * 1024)}MB limit.`);
            return false;
        }

        // Check if file is readable
        if (file.size === 0) {
            this.showError('The selected file appears to be empty.');
            return false;
        }

        return true;
    }

    updateFileDisplay(file) {
        const fileName = document.getElementById('fileName');
        const fileSize = document.getElementById('fileSize');
        const uploadIcon = document.getElementById('uploadIcon');
        
        if (fileName) fileName.textContent = file.name;
        if (fileSize) fileSize.textContent = this.formatFileSize(file.size);
        if (uploadIcon) {
            uploadIcon.className = 'fas fa-file-pdf text-danger';
        }

        // Update upload area appearance
        const uploadArea = document.getElementById('uploadArea');
        if (uploadArea) {
            uploadArea.classList.add('file-selected');
        }
    }

    formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    updateSliderValue(e) {
        const slider = e.target;
        const valueDisplay = document.getElementById(slider.id + 'Value');
        if (valueDisplay) {
            let value = slider.value;
            
            // Format value based on slider type
            if (slider.id.includes('threshold')) {
                value = (parseFloat(value) * 100).toFixed(0) + '%';
            } else if (slider.id.includes('timeout')) {
                value = value + 's';
            }
            
            valueDisplay.textContent = value;
        }
    }

    async handleFormSubmit(e) {
        e.preventDefault();
        
        const form = e.target;
        const formData = new FormData(form);
        
        // Validate form
        if (!this.validateForm(formData)) {
            return;
        }

        try {
            this.showLoading();
            
            const response = await fetch('/upload', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();
            
            if (response.ok) {
                this.currentJob = result.job_id;
                // Redirect to status page
                window.location.href = `/status/${result.job_id}`;
            } else {
                throw new Error(result.error || 'Upload failed');
            }
        } catch (error) {
            this.hideLoading();
            this.showError(`Upload failed: ${error.message}`);
        }
    }

    validateForm(formData) {
        const file = formData.get('file');
        if (!file || file.size === 0) {
            this.showError('Please select a PDF file to upload.');
            return false;
        }
        return true;
    }

    showLoading() {
        const existingOverlay = document.getElementById('loadingOverlay');
        if (existingOverlay) return;

        const overlay = document.createElement('div');
        overlay.id = 'loadingOverlay';
        overlay.className = 'loading-overlay';
        overlay.innerHTML = `
            <div class="text-center text-white">
                <div class="loading-spinner mb-3"></div>
                <h4>Processing Your PDF...</h4>
                <p>This may take a few moments</p>
            </div>
        `;
        document.body.appendChild(overlay);
    }

    hideLoading() {
        const overlay = document.getElementById('loadingOverlay');
        if (overlay) {
            overlay.remove();
        }
    }

    showError(message) {
        this.showAlert(message, 'danger');
    }

    showSuccess(message) {
        this.showAlert(message, 'success');
    }

    showInfo(message) {
        this.showAlert(message, 'info');
    }

    showAlert(message, type = 'info') {
        const alertContainer = document.getElementById('alertContainer') || document.body;
        
        const alert = document.createElement('div');
        alert.className = `alert alert-${type} alert-dismissible fade show`;
        alert.style.position = 'fixed';
        alert.style.top = '20px';
        alert.style.right = '20px';
        alert.style.zIndex = '9999';
        alert.style.maxWidth = '400px';
        
        alert.innerHTML = `
            <strong>${type === 'danger' ? 'Error!' : type === 'success' ? 'Success!' : 'Info:'}</strong>
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        
        alertContainer.appendChild(alert);
        
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            if (alert.parentNode) {
                alert.remove();
            }
        }, 5000);
    }

    // Status polling methods
    startStatusPolling(jobId, interval = 2000) {
        this.currentJob = jobId;
        this.statusPollingInterval = setInterval(() => {
            this.pollJobStatus(jobId);
        }, interval);
    }

    stopStatusPolling() {
        if (this.statusPollingInterval) {
            clearInterval(this.statusPollingInterval);
            this.statusPollingInterval = null;
        }
    }

    pauseStatusPolling() {
        if (this.statusPollingInterval) {
            clearInterval(this.statusPollingInterval);
        }
    }

    resumeStatusPolling() {
        if (this.currentJob && !this.statusPollingInterval) {
            this.startStatusPolling(this.currentJob);
        }
    }

    async pollJobStatus(jobId) {
        try {
            const response = await fetch(`/api/status/${jobId}`);
            const status = await response.json();
            
            this.updateStatusDisplay(status);
            
            // Stop polling if job is complete or failed
            if (status.status === 'completed' || status.status === 'error') {
                this.stopStatusPolling();
            }
        } catch (error) {
            console.error('Error polling status:', error);
        }
    }

    updateStatusDisplay(status) {
        // Update progress bar
        const progressBar = document.getElementById('progressBar');
        if (progressBar) {
            progressBar.style.width = status.progress + '%';
            progressBar.setAttribute('aria-valuenow', status.progress);
        }

        // Update status message
        const statusMessage = document.getElementById('statusMessage');
        if (statusMessage) {
            statusMessage.textContent = status.step || status.status;
        }

        // Update processing time
        const processingTime = document.getElementById('processingTime');
        if (processingTime && status.elapsed_time) {
            processingTime.textContent = `Elapsed time: ${status.elapsed_time}s`;
        }
    }

    // Utility methods
    formatTimestamp(timestamp) {
        return new Date(timestamp).toLocaleString();
    }

    copyToClipboard(text) {
        navigator.clipboard.writeText(text).then(() => {
            this.showSuccess('Copied to clipboard!');
        }).catch(() => {
            this.showError('Failed to copy to clipboard');
        });
    }

    downloadFile(url, filename) {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    // Page visibility handling
    handleVisibilityChange() {
        if (document.hidden) {
            this.pauseStatusPolling();
        } else {
            this.resumeStatusPolling();
        }
    }
}

// Initialize app when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    window.pdfApp = new PDFExtractorApp();
    
    // Handle page visibility changes
    document.addEventListener('visibilitychange', () => {
        window.pdfApp.handleVisibilityChange();
    });
});

// Global utility functions
function showConfirmDialog(title, message, callback) {
    const confirmed = confirm(`${title}\n\n${message}`);
    if (confirmed && typeof callback === 'function') {
        callback();
    }
    return confirmed;
}

function formatDuration(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainingSeconds = seconds % 60;
    
    if (hours > 0) {
        return `${hours}h ${minutes}m ${remainingSeconds}s`;
    } else if (minutes > 0) {
        return `${minutes}m ${remainingSeconds}s`;
    } else {
        return `${remainingSeconds}s`;
    }
}

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

function throttle(func, limit) {
    let inThrottle;
    return function() {
        const args = arguments;
        const context = this;
        if (!inThrottle) {
            func.apply(context, args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    }
}
