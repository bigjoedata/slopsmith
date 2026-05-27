/**
 * Tailwind CSS build config for slopsmith core.
 *
 * Replaces the Play CDN (`cdn.tailwindcss.com`) JIT runtime that
 * previously scanned the DOM ~1.8x/sec on the main thread, causing
 * sustained frame drops with the 3D highway. See slopsmith-desktop#110.
 *
 * Regenerate `static/tailwind.min.css` with:
 *   bash scripts/build-tailwind.sh
 *
 * The generated CSS is committed; there is no build step at serve time.
 */
module.exports = {
    content: [
        './static/**/*.{html,js}',
        './plugins/**/static/**/*.{html,js}',
        './plugins/**/screen.js',
        './plugins/**/settings.html',
        './plugins/**/*.html',
    ],
    theme: {
        extend: {
            colors: {
                dark: { 900: '#050508', 800: '#0a0a12', 700: '#10101e', 600: '#181830', 500: '#1e1e3a' },
                accent: { DEFAULT: '#4080e0', light: '#60a0ff', dark: '#2060b0' },
                gold: '#e8c040',
            },
            fontFamily: {
                display: ['"Inter"', 'system-ui', 'sans-serif'],
            },
        },
    },
    safelist: [
        // Dynamically-built class names that don't appear textually in
        // any source file the content globs cover.
        { pattern: /^(bg|text|border|ring)-(red|green|amber|yellow|blue|indigo|purple|pink|gray|slate)-(50|100|200|300|400|500|600|700|800|900)$/ },
        { pattern: /^(bg|text|border)-(dark|accent)(-.+)?$/ },
        'text-gold', 'bg-gold', 'border-gold',
    ],
    plugins: [],
};
