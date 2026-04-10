document.addEventListener('DOMContentLoaded', function () {
    // Auto-dismiss alerts after 5s
    document.querySelectorAll('.alert-dismissible').forEach(function (alert) {
        setTimeout(function () {
            bootstrap.Alert.getOrCreateInstance(alert).close();
        }, 5000);
    });

    // Scan forms — show loading overlay
    document.querySelectorAll('.scan-form').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            var overlay = document.getElementById('scan-overlay');
            if (overlay) {
                overlay.classList.remove('d-none');
            }
        });
    });

    // Bookmark toggle via AJAX
    document.querySelectorAll('.bookmark-form').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            var btn = form.querySelector('button');
            var icon = btn.querySelector('i');

            fetch(form.action, {
                method: 'POST',
                headers: { 'Accept': 'application/json' }
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.bookmarked) {
                    icon.className = 'bi bi-star-fill';
                    btn.classList.remove('text-muted', 'btn-outline-warning');
                    btn.classList.add('text-warning', 'btn-warning');
                } else {
                    icon.className = 'bi bi-star';
                    btn.classList.remove('text-warning', 'btn-warning');
                    btn.classList.add('text-muted', 'btn-outline-warning');
                }
                // Update button text if it has one
                var textNode = btn.childNodes[btn.childNodes.length - 1];
                if (textNode && textNode.nodeType === 3 && textNode.textContent.trim()) {
                    textNode.textContent = data.bookmarked ? ' Bookmarked' : ' Bookmark';
                }
            })
            .catch(function () {
                form.submit(); // Fallback to normal submit
            });
        });
    });
});
