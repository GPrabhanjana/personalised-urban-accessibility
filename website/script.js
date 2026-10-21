// Walk-time thresholds in minutes. Must match `levels` in the exported
// grid.geojson metadata (see src/02_grid/export_webapp.py).
const DISTANCE_LEVELS = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];
// Threshold is per-category (item.distanceIndex); this is only the default
// applied to newly added items. See section 3.2.4 of the paper.
const DEFAULT_DISTANCE_INDEX = DISTANCE_LEVELS.indexOf(9);   // 9 min
let currentDistanceIndex = DEFAULT_DISTANCE_INDEX;          // fallback only

// Global variables
let map;
let gridData = null;
let wardData = null;
let categories = [];
let selectedItems = [];
let currentMode = 'grid';
let gridLayer = null;
let wardLayer = null;
let currentScores = {};
let currentOpacity = 0.6;
let geocoder = null;
let currentTooltip = null;
let isDragging = false;
let isZooming = false;

// === GRID ADJUSTMENT RATIOS - EDIT THESE TO FIX ALIGNMENT ===
const GRID_ADJUSTMENT_RATIOS = {
    // Latitude adjustments (North-South)
    latMinRatio: 0.0,      // Adjust southern boundary (negative = move south, positive = move north)
    latMaxRatio: 0.0,      // Adjust northern boundary (negative = move south, positive = move north)
    
    // Longitude adjustments (East-West) 
    lngMinRatio: 0.0,      // Adjust western boundary (negative = move west, positive = move east)
    lngMaxRatio: 0.0,      // Adjust eastern boundary (negative = move west, positive = move east)
    
    // Uniform scaling (applies to all boundaries)
    uniformExpansion: 0.0, // Positive = expand all sides, negative = contract all sides
    
    // Individual cell size adjustments
    cellWidthRatio: -0.009,   // Adjust individual cell width
    cellHeightRatio: 0.012,  // Adjust individual cell height
    
    // Gap filling adjustments
    overlapCompensation: 0.0, // Small overlap to eliminate gaps (try 0.001 to 0.01)
    
    // Coordinate precision
    coordinatePrecision: 6    // Decimal places for coordinate rounding (6 = ~10cm precision)
};

// Two independent axes per item.
//
// 1. WEIGHT — how much it matters, relative to the other things you named.
const weightOptions = { "Standard": 1, "Preferred": 2 };
// Internal key stays "Preferred" so stored selections and the preset files
// keep working; WEIGHT_LABEL carries the wording the interface shows.
const WEIGHT_LABEL = { "Standard": "Standard", "Preferred": "Emphasised" };
const WEIGHT_SHORT = { "Standard": "Std", "Preferred": "Emph" };

// 2. CONSTRAINT — a single axis running from must-have to must-not-have.
//    negative  the item subtracts instead of adding, and stays out of the
//              denominator, so naming it can only lower a score, never raise it
//    hard      'require' zeroes a cell when k = 0; 'exclude' zeroes it when k > 0
const CONSTRAINTS = {
    none:     { label: 'Wanted',   short: '',      negative: false, hard: null,
                hint: 'adds to the score when it is within reach' },
    required: { label: 'Required', short: 'Req',   negative: false, hard: 'require',
                hint: 'must be within reach, or the score is 0' },
    avoid:    { label: 'Avoid',    short: 'Avoid', negative: true,  hard: null,
                hint: 'takes away from the score, more so the more of it is near' },
    never:    { label: 'Never',    short: 'Never', negative: true,  hard: 'exclude',
                hint: 'any one within reach makes the score 0' }
};
// Laid out 2x2: the wanted pair on the top row, the unwanted pair below.
const CONSTRAINT_ORDER = ['none', 'required', 'avoid', 'never'];

// Which controls apply to a given treatment.
//   weight  only where a relative importance means anything. Required and Never
//           are absolute statements, so "how much it matters" does not arise;
//           both carry unit weight.
//   decay   not for the negative treatments, since "how many nearby" is not a
//           question you ask about something you want none of.
const FIXED_WEIGHT = new Set(['required', 'never']);
function showsWeight(item) { return !FIXED_WEIGHT.has(constraintOf(item)); }
function showsDecay(item)  { return !isNegativeItem(item); }

// 3. HOW MANY NEARBY — what a second and third option add once you have one.
//    Internal keys are unchanged so stored selections and presets stay valid.
const decayOptions = {
    "Focused": 2 * Math.log(2),        // 2·ln(2) ≈ 1.386, half-life k=0.5
    "Balanced": Math.log(2),           // ln(2) ≈ 0.693,   half-life k=1
    "Expansive": Math.log(2) / 2       // ln(2)/2 ≈ 0.347, half-life k=2
};
const DECAY_LABEL = {
    "Focused": "A few", "Balanced": "A handful", "Expansive": "A lot"
};
const DECAY_SHORT = {
    "Focused": "Few", "Balanced": "Hand", "Expansive": "Lot"
};
const DECAY_ORDER = ['Focused', 'Balanced', 'Expansive'];

// Avoidance has no "how many" control: the question does not arise when you
// want none of something. The penalty accumulates at the middle rate.
const AVOID_LAMBDA = decayOptions["Balanced"];

function constraintOf(item) {
    return CONSTRAINTS[item && item.constraint] ? item.constraint : 'none';
}
function isNegativeItem(item) {
    return CONSTRAINTS[constraintOf(item)].negative;
}
function lambdaFor(item) {
    if (isNegativeItem(item)) return AVOID_LAMBDA;
    return decayOptions[item.decay] || decayOptions["Balanced"];
}
// Older selections stored the constraint inside the weight; fold it out.
function normaliseItem(item) {
    if (item.constraint === undefined) {
        const w = item.weight;
        item.constraint = w === 'Required' ? 'required'
                        : w === 'Avoided'  ? 'avoid'
                        : w === 'Excluded' ? 'never' : 'none';
    }
    if (!weightOptions[item.weight]) item.weight = 'Standard';
    if (!decayOptions[item.decay]) item.decay = 'Balanced';
    // The weight control is hidden for these, so clear any value carried over
    // from a previous treatment rather than let it apply invisibly.
    if (FIXED_WEIGHT.has(constraintOf(item))) item.weight = 'Standard';
    return item;
}

// Initialize the application
document.addEventListener('DOMContentLoaded', function() {
    suppressPressureWarnings();
    initializeMap();
    setupEventListeners();
    loadDataFiles();
    setupOpacityControl();
    setupGeocoder();
});

function suppressPressureWarnings() {
    const originalWarn = console.warn;
    console.warn = function(...args) {
        const message = args.join(' ');
        if (!message.includes('mozPressure') && !message.includes('MouseEvent.mozPressure')) {
            originalWarn.apply(console, args);
        }
    };
}

function initializeMap() {
    map = L.map('map', {
        preferCanvas: true,
        zoomControl: false,
        paddingTopLeft: [350, 0], // 350px for control panel width
        paddingBottomRight: [0, 0]
    }).setView([12.9716, 77.5946], 11);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
        maxZoom: 19,
        minZoom: 8,
        detectRetina: true,
        updateWhenIdle: false,
        keepBuffer: 2
    }).addTo(map);

    L.control.zoom({ position: 'bottomleft' }).addTo(map);
    
    setupMapEventHandlers();
}

function setupMapEventHandlers() {
    map.on('dragstart', function() {
        isDragging = true;
        closeCurrentTooltip();
    });
    
    map.on('dragend', function() {
        setTimeout(() => {
            isDragging = false;
        }, 100);
    });
    
    map.on('zoomstart', function() {
        isZooming = true;
        closeCurrentTooltip();
    });
    
    map.on('zoomend', function() {
        setTimeout(() => {
            isZooming = false;
        }, 50);
    });
    
    map.on('movestart', function() {
        closeCurrentTooltip();
    });
}

function closeCurrentTooltip() {
    if (currentTooltip) {
        currentTooltip.closeTooltip();
        currentTooltip = null;
    }
}

function setupEventListeners() {
    document.querySelectorAll('.mode-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            currentMode = this.dataset.mode;
            updateVisualization();
        });
    });

    // Modal walk-time sliders: label follows the handle, no map redraw needed
    // (the value is only read when the item is actually added).
    [['categoryDist','categoryDistLabel'], ['groupDist','groupDistLabel']].forEach(([s,l]) => {
        const sl = document.getElementById(s), lb = document.getElementById(l);
        if (sl && lb) {
            sl.max = DISTANCE_LEVELS.length - 1;
            sl.value = DEFAULT_DISTANCE_INDEX;
            lb.textContent = DISTANCE_LEVELS[DEFAULT_DISTANCE_INDEX] + ' min';
            sl.addEventListener('input', function () {
                lb.textContent = DISTANCE_LEVELS[parseInt(this.value)] + ' min';
            });
        }
    });

    // Search within the Create Group "available categories" list
    const groupSearch = document.getElementById('groupCategorySearch');
    if (groupSearch) groupSearch.addEventListener('input', filterGroupCategories);

    document.getElementById('categorySearch').addEventListener('input', filterCategories);

    window.addEventListener('click', function(event) {
        const categoryModal = document.getElementById('categoryModal');
        const groupModal = document.getElementById('groupModal');
        if (event.target === categoryModal) {
            closeCategoryModal();
        }
        if (event.target === groupModal) {
            closeGroupModal();
        }
    });
}

function setupOpacityControl() {
    const opacitySlider = document.getElementById('opacitySlider');
    const opacityValue = document.getElementById('opacityValue');
    
    opacitySlider.addEventListener('input', function() {
        currentOpacity = this.value / 100;
        opacityValue.textContent = this.value + '%';
        updateLayerOpacity();
    });
}

function setupGeocoder() {
    geocoder = L.Control.geocoder({
        defaultMarkGeocode: false,
        placeholder: 'Search for any place in Bangalore',
        collapsed: false,
        position: 'topright',
        suggestMinLength: 2,
        suggestTimeout: 300,
        geocoder: L.Control.Geocoder.nominatim({
            geocodingQueryParams: {
                countrycodes: 'in',
                bounded: 1,
                viewbox: '77.3910,12.7343,77.7920,13.1737',
                limit: 3,
                addressdetails: 1,
                extratags: 1
            }
        })
    })
    .on('markgeocode', function(e) {
        const bbox = e.geocode.bbox;
        const poly = L.polygon([
            [bbox.getSouthEast().lat, bbox.getSouthEast().lng],
            [bbox.getNorthEast().lat, bbox.getNorthEast().lng],
            [bbox.getNorthWest().lat, bbox.getNorthWest().lng],
            [bbox.getSouthWest().lat, bbox.getSouthWest().lng]
        ]);
        map.fitBounds(poly.getBounds());
       
        const marker = L.marker(e.geocode.center, {
            icon: L.divIcon({
                className: 'search-result-marker',
                html: '<div style="background: #3b82f6; width: 12px; height: 12px; border-radius: 50%; border: 3px solid white; box-shadow: 0 2px 8px rgba(0,0,0,0.3);"></div>',
                iconSize: [18, 18],
                iconAnchor: [9, 9]
            })
        })
        .addTo(map)
        .bindPopup(`
            <div style="font-size: 14px; font-weight: 500; color: #000000ff;">
                📍 ${e.geocode.name}
            </div>
        `)
        .openPopup();
       
        setTimeout(() => {
            if (map.hasLayer(marker)) {
                map.removeLayer(marker);
            }
        }, 4000);
    });
   
    const geocoderContainer = document.getElementById('geocoder');
    if (geocoderContainer) {
        geocoderContainer.appendChild(geocoder.onAdd(map));
    } else {
        geocoder.addTo(map);
    }

    // Enhanced persistence logic - run after geocoder is added to DOM
    setTimeout(() => {
        const geocoderElement = document.querySelector('.leaflet-control-geocoder');
        const formElement = document.querySelector('.leaflet-control-geocoder-form');
        const inputElement = document.querySelector('.leaflet-control-geocoder input');
        
        if (!geocoderElement || !formElement || !inputElement) {
            console.warn('Geocoder elements not found');
            return;
        }

        // Store original methods before overriding
        const originalMethods = {};
        
        // Override the _clearResults method that hides the geocoder
        if (geocoder._clearResults) {
            originalMethods._clearResults = geocoder._clearResults;
            geocoder._clearResults = function() {
                // Call original but don't let it hide the search bar
                const result = originalMethods._clearResults.call(this);
                
                // Force search bar to stay visible
                setTimeout(() => {
                    ensureGeocoderVisible();
                }, 0);
                
                return result;
            };
        }

        // Override the _geocode method
        if (geocoder._geocode) {
            originalMethods._geocode = geocoder._geocode;
            geocoder._geocode = function() {
                // Keep search bar visible during geocoding
                ensureGeocoderVisible();
                
                const result = originalMethods._geocode.apply(this, arguments);
                
                // Ensure it stays visible after geocoding
                setTimeout(() => {
                    ensureGeocoderVisible();
                }, 100);
                
                return result;
            };
        }

        // Override the collapse method
        if (geocoder.collapse) {
            originalMethods.collapse = geocoder.collapse;
            geocoder.collapse = function() {
                // Prevent collapse - just ensure visibility instead
                ensureGeocoderVisible();
                return this;
            };
        }

        // Enhanced visibility enforcement function
        function ensureGeocoderVisible() {
            // Force the main geocoder container to be visible
            if (geocoderElement) {
                geocoderElement.style.display = 'flex !important';
                geocoderElement.style.visibility = 'visible !important';
                geocoderElement.style.opacity = '1 !important';
                geocoderElement.classList.remove('leaflet-control-geocoder-collapsed');
                geocoderElement.classList.add('leaflet-control-geocoder-expanded');
            }

            // Ensure form is visible
            if (formElement) {
                formElement.style.display = 'block !important';
                formElement.style.visibility = 'visible !important';
                formElement.style.opacity = '1 !important';
            }

            // Ensure input is visible
            if (inputElement) {
                inputElement.style.display = 'block !important';
                inputElement.style.visibility = 'visible !important';
                inputElement.style.opacity = '1 !important';
            }

            // Handle suggestions container
            const alternativesElement = document.querySelector('.leaflet-control-geocoder-alternatives');
            if (alternativesElement && alternativesElement.children.length > 0) {
                alternativesElement.style.display = 'block !important';
                alternativesElement.style.visibility = 'visible !important';
                alternativesElement.style.opacity = '1 !important';
            }
        }

        // Initial visibility enforcement
        ensureGeocoderVisible();

        // Set up comprehensive monitoring
        const observer = new MutationObserver((mutations) => {
            let needsVisibilityCheck = false;
            
            mutations.forEach((mutation) => {
                if (mutation.type === 'attributes') {
                    const target = mutation.target;
                    
                    // Check if any visibility-related attributes changed
                    if (mutation.attributeName === 'style' || 
                        mutation.attributeName === 'class') {
                        
                        if (target === geocoderElement || 
                            target === formElement || 
                            target === inputElement ||
                            target.classList.contains('leaflet-control-geocoder-alternatives')) {
                            needsVisibilityCheck = true;
                        }
                    }
                }
            });
            
            if (needsVisibilityCheck) {
                setTimeout(() => {
                    ensureGeocoderVisible();
                }, 0);
            }
        });

        // Start observing the entire geocoder container and its children
        observer.observe(document.body, {
            attributes: true,
            subtree: true,
            attributeFilter: ['style', 'class']
        });

        // Additional event listeners to catch state changes
        if (inputElement) {
            // Prevent the input from being hidden on blur
            inputElement.addEventListener('blur', function() {
                setTimeout(() => {
                    ensureGeocoderVisible();
                }, 10);
            });

            // Ensure visibility on focus
            inputElement.addEventListener('focus', function() {
                ensureGeocoderVisible();
            });
        }

        // Periodic visibility check as failsafe
        const visibilityInterval = setInterval(() => {
            const currentElement = document.querySelector('.leaflet-control-geocoder');
            if (currentElement) {
                const computedStyle = window.getComputedStyle(currentElement);
                if (computedStyle.display === 'none' || 
                    computedStyle.visibility === 'hidden' || 
                    computedStyle.opacity === '0') {
                    ensureGeocoderVisible();
                }
            } else {
                // If geocoder disappeared completely, clear the interval
                clearInterval(visibilityInterval);
            }
        }, 1000);

        // Store cleanup function globally if needed
        window.geocoderCleanup = () => {
            clearInterval(visibilityInterval);
            observer.disconnect();
        };

        console.log('Enhanced geocoder persistence enabled');
        
    }, 200);
}

function updateLayerOpacity() {
    if (gridLayer) {
        gridLayer.eachLayer(function(layer) {
            if (layer.setStyle) {
                layer.setStyle({ fillOpacity: currentOpacity });
            }
        });
    }
    
    if (wardLayer) {
        wardLayer.eachLayer(function(layer) {
            if (layer.setStyle) {
                layer.setStyle({ fillOpacity: currentOpacity });
            }
        });
    }
}

function loadDataFiles() {
    loadGridData();
    loadWardData();
}

function loadGridData() {
    fetch('grid.geojson')
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(geojson => {
            processGridData(geojson);
            console.log(`Grid data loaded (${Object.keys(gridData.grid_cells).length} cells, ${categories.length} categories)`);
        })
        .catch(error => {
            console.error('Error loading grid data:', error);
        });
}

function loadWardData() {
    fetch('wards.geojson')
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(geojson => {
            wardData = geojson;
            console.log('Ward data loaded successfully');
        })
        .catch(error => {
            console.error('Error loading ward data:', error);
        });
}

function processGridData(geojson) {
    if (!geojson || !Array.isArray(geojson.features)) {
        throw new Error('Invalid GeoJSON: missing features array');
    }

    gridData = {
        grid_cells: {},
        grid_scores: {},  // Keep original name for compatibility - contains k values
        ward_assignments: {},
        ward_statistics: {}
    };

    categories = [];
    const catSet = new Set();

    geojson.features.forEach(f => {
        const props = f.properties || {};
        const cell_id = (props.cell_id !== undefined) ? String(props.cell_id) : 
                       (props.cellId !== undefined ? String(props.cellId) : null);
        
        if (!cell_id) {
            console.warn('Skipping feature without cell_id', props);
            return;
        }

        const geometry = f.geometry || null;
        const bboxInfo = computeBoundsAndCenter(geometry);
        const bounds = bboxInfo ? bboxInfo.bounds : null;
        const center = bboxInfo ? bboxInfo.center : null;
        const row = (props.row !== undefined) ? props.row : -1;
        const col = (props.col !== undefined) ? props.col : -1;
        const ward_name = props.ward_name || props.wardName || props.ward || null;

        // The field is still called "scores" but now contains k values
        let k_values = props.scores || {};
        if (typeof k_values !== 'object' || k_values === null) {
            try {
                const parsed = JSON.parse(k_values);
                if (typeof parsed === 'object') {
                    k_values = parsed;
                }
            } catch {
                k_values = {};
            }
        }

        gridData.grid_cells[cell_id] = {
            geometry: geometry,
            bounds: bounds,
            center: center,
            row: row,
            col: col
        };

        // Store k values (field still called "scores" but contains k values)
        Object.entries(k_values).forEach(([cat, val]) => {
            if (!gridData.grid_scores[cat]) gridData.grid_scores[cat] = {};
            // Store as-is: array of k per threshold, or a legacy scalar.
            // getKValue() handles both.
            gridData.grid_scores[cat][cell_id] = Array.isArray(val)
                ? val.map(v => Number(v) || 0)
                : Number(val) || 0;
            catSet.add(cat);
        });

        if (ward_name) {
            gridData.ward_assignments[cell_id] = ward_name;
            if (!gridData.ward_statistics[ward_name]) {
                gridData.ward_statistics[ward_name] = { cell_ids: [], cell_count: 0 };
            }
            gridData.ward_statistics[ward_name].cell_ids.push(cell_id);
            gridData.ward_statistics[ward_name].cell_count++;
        }
    });

    categories = Array.from(catSet).sort();
    populateCategoryList();
}

// === NEW: RATIO-BASED BOUNDS ADJUSTMENT FUNCTION ===
function adjustBoundsWithRatios(originalBounds) {
    if (!originalBounds) return null;
    
    const [minLng, minLat, maxLng, maxLat] = originalBounds;
    const ratios = GRID_ADJUSTMENT_RATIOS;
    
    // Calculate cell dimensions for ratio calculations
    const cellWidth = maxLng - minLng;
    const cellHeight = maxLat - minLat;
    
    // Apply individual boundary adjustments
    let adjustedMinLng = minLng + (cellWidth * ratios.lngMinRatio);
    let adjustedMaxLng = maxLng + (cellWidth * ratios.lngMaxRatio);
    let adjustedMinLat = minLat + (cellHeight * ratios.latMinRatio);
    let adjustedMaxLat = maxLat + (cellHeight * ratios.latMaxRatio);
    
    // Apply uniform expansion/contraction
    if (ratios.uniformExpansion !== 0) {
        const expansionLng = cellWidth * ratios.uniformExpansion;
        const expansionLat = cellHeight * ratios.uniformExpansion;
        adjustedMinLng -= expansionLng;
        adjustedMaxLng += expansionLng;
        adjustedMinLat -= expansionLat;
        adjustedMaxLat += expansionLat;
    }
    
    // Apply cell size adjustments
    if (ratios.cellWidthRatio !== 0) {
        const widthAdjustment = cellWidth * ratios.cellWidthRatio * 0.5;
        adjustedMinLng -= widthAdjustment;
        adjustedMaxLng += widthAdjustment;
    }
    
    if (ratios.cellHeightRatio !== 0) {
        const heightAdjustment = cellHeight * ratios.cellHeightRatio * 0.5;
        adjustedMinLat -= heightAdjustment;
        adjustedMaxLat += heightAdjustment;
    }
    
    // Apply overlap compensation (small expansion to eliminate gaps)
    if (ratios.overlapCompensation !== 0) {
        const overlapLng = cellWidth * ratios.overlapCompensation * 0.5;
        const overlapLat = cellHeight * ratios.overlapCompensation * 0.5;
        adjustedMinLng -= overlapLng;
        adjustedMaxLng += overlapLng;
        adjustedMinLat -= overlapLat;
        adjustedMaxLat += overlapLat;
    }
    
    // Round coordinates to specified precision
    const precision = ratios.coordinatePrecision;
    const factor = Math.pow(10, precision);
    adjustedMinLng = Math.round(adjustedMinLng * factor) / factor;
    adjustedMinLat = Math.round(adjustedMinLat * factor) / factor;
    adjustedMaxLng = Math.round(adjustedMaxLng * factor) / factor;
    adjustedMaxLat = Math.round(adjustedMaxLat * factor) / factor;
    
    return [adjustedMinLng, adjustedMinLat, adjustedMaxLng, adjustedMaxLat];
}

function adjustGeometryWithRatios(geometry) {
    if (!geometry) return null;
    
    const ratios = GRID_ADJUSTMENT_RATIOS;
    
    // If no adjustments needed, return original
    const hasAdjustments = Object.values(ratios).some(val => 
        typeof val === 'number' && val !== 0
    );
    if (!hasAdjustments) return geometry;
    
    // Clone geometry to avoid modifying original
    const adjustedGeometry = JSON.parse(JSON.stringify(geometry));
    
    // First, calculate the bounding box of the geometry
    const bounds = computeBoundsFromGeometry(adjustedGeometry);
    if (!bounds) return geometry;
    
    const [minLng, minLat, maxLng, maxLat] = bounds;
    const centerLng = (minLng + maxLng) / 2;
    const centerLat = (minLat + maxLat) / 2;
    const currentWidth = maxLng - minLng;
    const currentHeight = maxLat - minLat;
    
    // Calculate new dimensions based on ratios
    let newWidth = currentWidth;
    let newHeight = currentHeight;
    
    // Apply individual size adjustments
    if (ratios.cellWidthRatio !== 0) {
        newWidth = currentWidth * (1 + ratios.cellWidthRatio);
    }
    
    if (ratios.cellHeightRatio !== 0) {
        newHeight = currentHeight * (1 + ratios.cellHeightRatio);
    }
    
    // Apply uniform expansion/contraction
    if (ratios.uniformExpansion !== 0) {
        newWidth = newWidth * (1 + ratios.uniformExpansion);
        newHeight = newHeight * (1 + ratios.uniformExpansion);
    }
    
    // Apply overlap compensation (small expansion to eliminate gaps)
    if (ratios.overlapCompensation !== 0) {
        newWidth = newWidth * (1 + ratios.overlapCompensation);
        newHeight = newHeight * (1 + ratios.overlapCompensation);
    }
    
    // Calculate scaling factors
    const widthScale = newWidth / currentWidth;
    const heightScale = newHeight / currentHeight;
    
    // Apply boundary shift adjustments to center point
    let adjustedCenterLng = centerLng;
    let adjustedCenterLat = centerLat;
    
    if (ratios.lngMinRatio !== 0 || ratios.lngMaxRatio !== 0) {
        const avgLngShift = (ratios.lngMinRatio + ratios.lngMaxRatio) / 2;
        adjustedCenterLng += currentWidth * avgLngShift;
    }
    
    if (ratios.latMinRatio !== 0 || ratios.latMaxRatio !== 0) {
        const avgLatShift = (ratios.latMinRatio + ratios.latMaxRatio) / 2;
        adjustedCenterLat += currentHeight * avgLatShift;
    }
    
    // Function to transform coordinates
    function transformCoordinates(coords) {
        if (typeof coords[0] === 'number') {
            // Single coordinate pair [lng, lat]
            const [lng, lat] = coords;
            
            // Translate to origin (relative to center)
            const relLng = lng - centerLng;
            const relLat = lat - centerLat;
            
            // Scale
            const scaledLng = relLng * widthScale;
            const scaledLat = relLat * heightScale;
            
            // Translate back to adjusted center
            let finalLng = scaledLng + adjustedCenterLng;
            let finalLat = scaledLat + adjustedCenterLat;
            
            // Round to precision
            const precision = ratios.coordinatePrecision;
            const factor = Math.pow(10, precision);
            finalLng = Math.round(finalLng * factor) / factor;
            finalLat = Math.round(finalLat * factor) / factor;
            
            return [finalLng, finalLat];
        } else {
            // Array of coordinates - recursively transform
            return coords.map(transformCoordinates);
        }
    }
    
    // Apply transformations to geometry coordinates
    if (adjustedGeometry.coordinates) {
        adjustedGeometry.coordinates = transformCoordinates(adjustedGeometry.coordinates);
    }
    
    return adjustedGeometry;
}

function computeBoundsFromGeometry(geometry) {
    const coords = [];

    function collectCoords(g) {
        if (!g) return;
        if (g.type === 'Point') {
            coords.push(g.coordinates);
        } else if (g.type === 'Polygon') {
            g.coordinates.forEach(ring => ring.forEach(pt => coords.push(pt)));
        } else if (g.type === 'MultiPolygon') {
            g.coordinates.forEach(poly => poly.forEach(ring => ring.forEach(pt => coords.push(pt))));
        } else if (g.type === 'LineString') {
            g.coordinates.forEach(pt => coords.push(pt));
        } else if (g.type === 'MultiLineString') {
            g.coordinates.forEach(line => line.forEach(pt => coords.push(pt)));
        } else if (g.type === 'GeometryCollection') {
            (g.geometries || []).forEach(collectCoords);
        }
    }

    collectCoords(geometry);

    if (coords.length === 0) return null;

    let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
    coords.forEach(pt => {
        const lng = pt[0], lat = pt[1];
        if (lng < minLng) minLng = lng;
        if (lng > maxLng) maxLng = lng;
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
    });

    return [minLng, minLat, maxLng, maxLat];
}

function computeBoundsAndCenter(geometry) {
    const coords = [];

    function collectCoords(g) {
        if (!g) return;
        if (g.type === 'Point') {
            coords.push(g.coordinates);
        } else if (g.type === 'Polygon') {
            g.coordinates.forEach(ring => ring.forEach(pt => coords.push(pt)));
        } else if (g.type === 'MultiPolygon') {
            g.coordinates.forEach(poly => poly.forEach(ring => ring.forEach(pt => coords.push(pt))));
        } else if (g.type === 'MultiLineString' || g.type === 'LineString') {
            (g.coordinates || []).forEach(pt => coords.push(pt));
        } else if (g.type === 'GeometryCollection') {
            (g.geometries || []).forEach(collectCoords);
        }
    }

    collectCoords(geometry);

    if (coords.length === 0) return null;

    let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
    coords.forEach(pt => {
        const lng = pt[0], lat = pt[1];
        if (lng < minLng) minLng = lng;
        if (lng > maxLng) maxLng = lng;
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
    });

    const center = [(minLng + maxLng) / 2, (minLat + maxLat) / 2];
    return {
        bounds: [minLng, minLat, maxLng, maxLat],
        center: center
    };
}

function createTooltipLayer(layer, tooltipContent, isGrid = true) {
    layer.on('mouseover', function(e) {
        if (isDragging || isZooming) return;
        
        closeCurrentTooltip();
        currentTooltip = layer;
        layer.openTooltip(e.latlng);
    });
    
    layer.on('mouseout', function() {
        if (!isDragging && !isZooming) {
            layer.closeTooltip();
            if (currentTooltip === layer) {
                currentTooltip = null;
            }
        }
    });
    
    layer.bindTooltip(tooltipContent, { 
        permanent: false, 
        direction: 'top', 
        className: isGrid ? 'grid-tooltip' : 'ward-tooltip',
        sticky: !isGrid,
        opacity: 0.9
    });
}

function addSelectedCategories() {
    const checkedItems = document.querySelectorAll('#categoryList .category-item.selected');
    const weight = document.getElementById('categoryWeight').value;
    const constraintEl = document.getElementById('categoryConstraint');
    const constraint = constraintEl ? constraintEl.value : 'none';
    const decay = document.getElementById('categoryDecay').value; // New decay selection
    const distEl = document.getElementById('categoryDist');
    const distanceIndex = distEl ? parseInt(distEl.value) : DEFAULT_DISTANCE_INDEX;
    
    if (checkedItems.length === 0) {
        alert('Please select at least one category!');
        return;
    }
    
    checkedItems.forEach(item => {
        const categoryName = item.dataset.category;
        // Keyed on constraint as well as name. NOTE: this alone does not let a
        // category be added twice under different treatments - the picker does
        // not offer an already-selected category a second time, so that case is
        // unreachable in practice and is documented as a limitation rather than
        // a feature. The key is left as-is so the scoring path stays correct if
        // the picker is ever changed.
        const existingItem = selectedItems.find(it =>
            it.type === 'feature' && it.name === categoryName
            && constraintOf(it) === constraint
        );

        if (!existingItem) {
            selectedItems.push(normaliseItem({
                type: 'feature',
                name: categoryName,
                data: [categoryName],
                weight: weight,
                constraint: constraint,
                decay: decay,
                distanceIndex: distanceIndex   // per-category walk time
            }));
        }
    });
    
    updateSelectedItemsDisplay();
    updateVisualization();
    closeCategoryModal();
}

function filterGroupCategories() {
    const q = (document.getElementById('groupCategorySearch').value || '').toLowerCase();
    document.querySelectorAll('#availableCategories .category-item').forEach(it => {
        const name = (it.dataset.category || '').toLowerCase();
        it.style.display = name.includes(q) ? '' : 'none';
    });
}

function nextGroupName() {
    let n = 1;
    const taken = new Set(selectedItems.filter(i => i.type === 'group').map(i => i.name));
    while (taken.has('Group ' + n)) n++;
    return 'Group ' + n;
}

function createGroup() {
    const nameEl = document.getElementById('groupName');
    const groupName = (nameEl.value.trim() || nameEl.placeholder || nextGroupName());
    const groupDistEl = document.getElementById('groupDist');
    const groupDistanceIndex = groupDistEl ? parseInt(groupDistEl.value) : DEFAULT_DISTANCE_INDEX;
    const weight = document.getElementById('groupWeight').value;
    const groupConstraintEl = document.getElementById('groupConstraint');
    const constraint = groupConstraintEl ? groupConstraintEl.value : 'none';
    const decay = document.getElementById('groupDecay').value; // New decay selection
    const selectedCats = document.querySelectorAll('#selectedCategories .category-item');
    
    if (!groupName) {
        alert('Please enter a group name!');
        return;
    }
    
    if (selectedCats.length === 0) {
        alert('Please select at least one category for the group!');
        return;
    }
    
    const groupCategories = Array.from(selectedCats).map(item => 
        item.querySelector('span').textContent
    );
    
    const existingGroup = selectedItems.find(item => 
        item.type === 'group' && item.name === groupName
    );
    
    if (!existingGroup) {
        selectedItems.push(normaliseItem({
            type: 'group',
            name: groupName,
            data: groupCategories,
            weight: weight,
            constraint: constraint,
            decay: decay,
            distanceIndex: groupDistanceIndex   // per-group walk time
        }));
    }
    
    updateSelectedItemsDisplay();
    updateVisualization();
    closeGroupModal();
    
    document.getElementById('groupName').value = '';
    document.getElementById('selectedCategories').innerHTML = '';
    populateGroupCategories();
}

// Hide the controls that do not bear on the chosen treatment, so the add panel
// offers the same set as the edit dropdown for the same choice.
function syncAddPanel(scope) {
    const sel = document.getElementById(scope + 'Constraint');
    if (!sel) return;
    const probe = { constraint: sel.value };
    const show = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.style.display = on ? '' : 'none';
    };
    show(scope + 'WeightWrap', showsWeight(probe));
    show(scope + 'DecayWrap', showsDecay(probe));
}

// Compact summary shown on the item's button, listing only the parts that
// apply to the chosen treatment.
function itemSummary(item) {
    const c = CONSTRAINTS[constraintOf(item)];
    const parts = [];
    if (c.short) parts.push(c.short);
    if (showsWeight(item)) parts.push(WEIGHT_SHORT[item.weight] || 'Std');
    if (showsDecay(item)) parts.push(DECAY_SHORT[item.decay] || 'Hand');
    parts.push(DISTANCE_LEVELS[item.distanceIndex ?? DEFAULT_DISTANCE_INDEX]);
    return parts.join('+');
}

function updateSelectedItemsDisplay() {
    const container = document.getElementById('selectedItems');
    container.innerHTML = '';
    
    selectedItems.forEach((item, index) => {
        normaliseItem(item);
        const div = document.createElement('div');
        const negative = isNegativeItem(item);
        div.className = 'selected-item ' + (negative ? 'avoid' : 'want');
        div.dataset.index = index;

        const displayText = item.type === 'feature' ?
            item.name :
            `${item.name} (${item.data.length} cats)`;

        const distShort = DISTANCE_LEVELS[item.distanceIndex ?? DEFAULT_DISTANCE_INDEX];
        div.innerHTML = `
            <span class="item-name" style="flex: 1; min-width: 0;">${displayText}</span>
            <div class="custom-dropdown" style="position: relative; min-width: 128px;">
                <button class="dropdown-btn weight-selector" onclick="toggleCustomDropdown(${index})" style="width: 100%; ">
                    ${itemSummary(item)} ▼
                </button>
            </div>
        `;
        
        // Click handler for item selection (excluding dropdown)
        div.addEventListener('click', function(e) {
            if (e.target.closest('.custom-dropdown')) {
                return;
            }
            this.classList.toggle('selected');
        });
        
        container.appendChild(div);
    });
}

function toggleCustomDropdown(index) {
    // Close all other dropdowns first
    document.querySelectorAll('.settings-dropdown').forEach(dropdown => {
        document.body.removeChild(dropdown);
    });
    
    // Find the button that was clicked
    const button = document.querySelector(`[onclick="toggleCustomDropdown(${index})"]`);
    const rect = button.getBoundingClientRect();
    
    // Calculate dropdown dimensions and available space
    const dropdownHeight = 200; // Approximate height of the dropdown
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;
    const shouldFlipUp = spaceBelow < (dropdownHeight + 100) && spaceAbove > dropdownHeight;
    
    // Create dropdown as a separate element attached to body
    const dropdown = document.createElement('div');
    dropdown.className = 'settings-dropdown';
    
    // Position dropdown above or below based on available space
    const topPosition = shouldFlipUp ? rect.top - dropdownHeight - 2 : rect.bottom + 2;
    
    dropdown.style.cssText = `
        position: fixed;
        left: ${rect.left - 20}px;
        top: ${topPosition}px;
        background: white;
        border: 1px solid #ddd;
        border-radius: 4px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        z-index: 1000;
        width: 200px;
        box-sizing: border-box;
        padding: 8px 0;
        max-height: 320px;
        overflow-x: hidden;
        overflow-y: auto;
    `;
    
    const item = normaliseItem(selectedItems[index]);
    const head = t => `<div style="padding: 8px 12px; font-weight: bold; border-bottom: 1px solid #eee; border-top: 1px solid #eee; font-size: 12px; color: #666; margin-top: 4px;">${t}</div>`;

    // Sections read as parallel questions, and only those that bear on the
    // chosen treatment are shown.
    dropdown.innerHTML = `
        <div style="padding: 8px 12px 4px; font-weight: bold; font-size: 12px; color: #666;">HOW IT'S TREATED</div>
        <div class="constraint-segment" style="display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 0 12px 8px;">
            ${CONSTRAINT_ORDER.map(v => {
                const c = CONSTRAINTS[v];
                const on = constraintOf(item) === v;
                const tone = c.negative ? '#c74848' : '#3aa76d';
                return `<div class="dropdown-option" data-type="constraint" data-value="${v}"
                    data-index="${index}" title="${c.hint}"
                    style="text-align:center; padding:5px 4px; cursor:pointer;
                           border-radius:6px; font-size:11px; white-space:nowrap;
                           overflow:hidden; text-overflow:ellipsis;
                           border:1px solid ${on ? tone : '#ddd'};
                           background:${on ? tone : '#fff'}; color:${on ? '#fff' : '#555'};">
                    ${c.label}</div>`;
            }).join('')}
        </div>
        ${!showsWeight(item) ? '' : `
        ${head('HOW MUCH IT MATTERS')}
        ${['Standard', 'Preferred'].map(v => `
        <div class="dropdown-option" data-type="weight" data-value="${v}" data-index="${index}" style="padding: 6px 12px; cursor: pointer; ${item.weight === v ? 'background: #c9ebffff;' : ''}">${WEIGHT_LABEL[v]}</div>`).join('')}
        `}
        ${!showsDecay(item) ? '' : `
        ${head('HOW MANY NEARBY')}
        ${DECAY_ORDER.map(v => `
        <div class="dropdown-option" data-type="decay" data-value="${v}" data-index="${index}" style="padding: 6px 12px; cursor: pointer; ${item.decay === v ? 'background: #c9ebffff;' : ''}">${DECAY_LABEL[v]}</div>`).join('')}
        `}

        ${head("HOW FAR YOU'LL WALK")}
        <div style="padding: 8px 12px;">
            <input type="range" id="itemDist_${index}" min="0" max="${DISTANCE_LEVELS.length - 1}"
                   step="1" value="${item.distanceIndex ?? DEFAULT_DISTANCE_INDEX}"
                   style="width: 100%; height: 6px; border-radius: 3px;">
            <div style="display:flex; justify-content:space-between; font-size:10px; color:#8e9aaf; margin-top:2px;">
                <span>${DISTANCE_LEVELS[0]}</span><span>${DISTANCE_LEVELS[DISTANCE_LEVELS.length-1]} min</span>
            </div>
            <div id="itemDistLabel_${index}" style="text-align:center; font-size:12px; font-weight:600; color:#2c3e50; margin-top:4px;">
                ${DISTANCE_LEVELS[item.distanceIndex ?? DEFAULT_DISTANCE_INDEX]} min
            </div>
        </div>
    `;
    
    document.body.appendChild(dropdown);
    
    // Get actual dropdown height after it's rendered
    const actualHeight = dropdown.offsetHeight;
    
    // Adjust position if needed based on actual height
    if (shouldFlipUp) {
        dropdown.style.top = `${rect.top - actualHeight - 10}px`;
    }
    
    // Walk-time slider: live-updates without closing the dropdown
    const distInput = dropdown.querySelector(`#itemDist_${index}`);
    if (distInput) {
        // 'input' only moves the labels - re-rendering the whole grid on every
        // intermediate value makes dragging unusable. The map redraws on
        // 'change', which fires when the handle is released.
        const paintLabels = function () {
            const idx = parseInt(distInput.value);
            selectedItems[index].distanceIndex = idx;
            const lab = dropdown.querySelector(`#itemDistLabel_${index}`);
            if (lab) lab.textContent = DISTANCE_LEVELS[idx] + ' min';
            const btn = document.querySelector(`[onclick="toggleCustomDropdown(${index})"]`);
            if (btn) btn.textContent = `${itemSummary(selectedItems[index])} ▼`;
        };
        distInput.addEventListener('input',  function (e) { e.stopPropagation(); paintLabels(); });
        distInput.addEventListener('change', function (e) {
            e.stopPropagation(); paintLabels(); updateVisualization();
        });
        distInput.addEventListener('click', e => e.stopPropagation());
    }

    // Add hover effects and click handlers for dropdown options
    dropdown.querySelectorAll('.dropdown-option').forEach(option => {
        option.addEventListener('mouseenter', function () {
    const bg = this.style.backgroundColor;
    if (!bg || bg === 'white' || bg === 'rgb(255, 255, 255)') {
        this.style.backgroundColor = '#f8f9fa'; // hover color
    }
});

option.addEventListener('mouseleave', function () {
    const bg = this.style.backgroundColor;
    if (bg === '#f8f9fa' || bg === 'rgb(248, 249, 250)') {
        this.style.backgroundColor = 'white'; // reset
    }
});

        
        option.addEventListener('click', function() {
            const index = parseInt(this.dataset.index);
            const type = this.dataset.type;
            const value = this.dataset.value;

            selectedItems[index][type] = value;
            document.body.removeChild(dropdown);
            updateSelectedItemsDisplay();
            updateVisualization();

            // Switching between wanting and avoiding changes which controls
            // apply, so reopen the panel rather than making the user click back.
            if (type === 'constraint') setTimeout(() => toggleCustomDropdown(index), 0);
        });
    });
    
    // Close dropdown when clicking outside
    setTimeout(() => {
        document.addEventListener('click', function closeDropdown(e) {
            if (!dropdown.contains(e.target) && e.target !== button) {
                if (document.body.contains(dropdown)) {
                    document.body.removeChild(dropdown);
                }
                document.removeEventListener('click', closeDropdown);
            }
        });
    }, 0);
}

function selectAllItems() {
    const items = document.querySelectorAll('.selected-item');
    items.forEach(item => {
        item.classList.add('selected');
    });
}

function removeSelectedItems() {
    const selectedElements = document.querySelectorAll('.selected-item.selected');
    const indicesToRemove = Array.from(selectedElements).map(el => parseInt(el.dataset.index));
    
    if (indicesToRemove.length === 0) {
        alert('Please select items to remove!');
        return;
    }
    
    indicesToRemove.sort((a, b) => b - a).forEach(index => {
        selectedItems.splice(index, 1);
    });
    
    updateSelectedItemsDisplay();
    updateVisualization();
}

function updateVisualization() {
    if (!gridData) return;
    if (currentMode === 'ward' && !wardData) return;
    
    if (!selectedItems.length) {
        clearVisualization();
        return;
    }
    
    const currentCenter = map.getCenter();
    const currentZoom = map.getZoom();
    
    showLoading();
    
    setTimeout(() => {
        try {
            const gridScores = calculateWeightedScores();
            currentScores = gridScores;
            
            if (currentMode === 'grid') {
                visualizeGridData(gridScores);
            } else {
                const wardScores = calculateWardScores(gridScores);
                visualizeWardData(wardScores);
            }
            
            map.setView(currentCenter, currentZoom);
        } catch (error) {
            console.error('Visualization error:', error);
        }
        
        hideLoading();
    }, 100);
}

// === UPDATED: GRID VISUALIZATION WITH CATEGORY COUNTS IN TOOLTIP ===
function visualizeGridData(scores) {
    clearLayers();
    const polygons = [];

    for (const [cellId, cellData] of Object.entries(gridData.grid_cells)) {
        const score = scores && scores[cellId] !== undefined ? scores[cellId] : 0;

        let geoJsonLayer;
        if (cellData.geometry) {
            // Use adjusted geometry
            const adjustedGeometry = adjustGeometryWithRatios(cellData.geometry);
            const feature = {
                type: "Feature",
                geometry: adjustedGeometry,
                properties: {}
            };

            geoJsonLayer = L.geoJSON(feature, {
                style: function() {
                    // Changed normalization: scores are already 0-1, so no division by 2
                    const normalizedScore = Math.min(Math.max(score, 0), 1);
                    const color = getColorFromScore(normalizedScore);

                    return {
                        fillColor: color,
                        weight: 0, // Remove stroke to eliminate visual gaps
                        opacity: 0,
                        color: 'transparent',
                        fillOpacity: currentOpacity
                    };
                }
            });
        } else if (cellData.bounds) {
            // Use adjusted bounds for rectangle fallback
            const adjustedBounds = adjustBoundsWithRatios(cellData.bounds);
            if (adjustedBounds) {
                const [minLng, minLat, maxLng, maxLat] = adjustedBounds;
                const latLngBounds = [[minLat, minLng], [maxLat, maxLng]];

                // Changed normalization: scores are already 0-1, so no division by 2
                const normalizedScore = Math.min(Math.max(score, 0), 1);
                const color = getColorFromScore(normalizedScore);

                geoJsonLayer = L.rectangle(latLngBounds, {
                    color: 'transparent',
                    fillColor: color,
                    fillOpacity: currentOpacity,
                    weight: 0 // Remove stroke
                });
            } else {
                continue;
            }
        } else {
            continue;
        }

        const wardName = gridData.ward_assignments[cellId] || 'Unknown';
        
        // Create detailed tooltip with category counts
        const tooltipContent = createDetailedTooltip(cellId, score, wardName);
        createTooltipLayer(geoJsonLayer, tooltipContent, true);

        polygons.push(geoJsonLayer);
    }

    gridLayer = L.layerGroup(polygons).addTo(map);
    
    // Console log for debugging adjustments
    console.log('Grid rendered with ratios:', GRID_ADJUSTMENT_RATIOS);
}

// NEW: Function to create simple tooltip with amenity counts
function createDetailedTooltip(cellId, totalScore, wardName) {
    let tooltipHTML = `<div style="min-width: 180px;">`;
    tooltipHTML += `<strong>Grid Score: ${totalScore.toFixed(4)}</strong><br>`;
    tooltipHTML += `<strong>Ward: ${wardName}</strong><br>`;
    
    // Add simple amenity list for selected items
    if (selectedItems.length > 0) {
        tooltipHTML += `<br>`;
        
        // Collect all unique categories from selected items
        const allCategories = new Set();
        selectedItems.forEach(item => {
            item.data.forEach(category => {
                allCategories.add(category);
            });
        });
        
        // Display each category with its count
        Array.from(allCategories).sort().forEach(category => {
            const count = getCategoryCount(cellId, category);
            tooltipHTML += `${category}: ${count}<br>`;
        });
    }
    
    tooltipHTML += `</div>`;
    return tooltipHTML;
}

// Read k for a category at a given walk-time threshold.
// Accepts both the array format (one k per level) and the legacy scalar.
function getKValue(category, cellId, distanceIndex) {
    const scores = gridData.grid_scores[category];
    if (!scores || scores[cellId] === undefined) return 0;
    const val = scores[cellId];
    if (Array.isArray(val)) {
        const idx = Math.min(distanceIndex ?? currentDistanceIndex, val.length - 1);
        return val[idx] || 0;
    }
    return Number(val) || 0;
}

// Helper function to get category count for a cell, at the active threshold
function getCategoryCount(cellId, category) {
    return getKValue(category, cellId, currentDistanceIndex);
}

function visualizeWardData(wardScores) {
    clearLayers();
    
    if (!wardData || !wardScores) return;
    
    const allWardNames = wardData.features.map(f => {
        const props = f.properties;
        return props.Ward_Name || props.Name || props.name || props.NAME || 'Unknown';
    });
    
    allWardNames.forEach(wardName => {
        if (!(wardName in wardScores)) {
            wardScores[wardName] = 0;
        }
    });
    
    const polygons = [];
    
    wardData.features.forEach(feature => {
        const props = feature.properties;
        const wardName = props.Ward_Name || props.Name || props.name || props.NAME || 'Unknown';
        const score = wardScores[wardName] || 0;
        
        const geoJsonLayer = L.geoJSON(feature, {
            style: function() {
                // Changed normalization: scores are already 0-1, so no division by 2
                const normalizedScore = Math.min(Math.max(score, 0), 1);
                const color = getColorFromScore(normalizedScore);
                
                return {
                    fillColor: color,
                    weight: 2,
                    opacity: 1,
                    color: 'white',
                    dashArray: '3',
                    fillOpacity: currentOpacity
                };
            }
        });
        
        const tooltipContent = `${wardName}<br>Ward Score: ${score.toFixed(4)}`;
        createTooltipLayer(geoJsonLayer, tooltipContent, false);
        
        polygons.push(geoJsonLayer);
    });
    
    wardLayer = L.layerGroup(polygons).addTo(map);
}

function openCategoryModal() {
    if (!categories.length) {
        alert('Please wait for grid data to load!');
        return;
    }
    
    populateCategoryList();
    syncAddPanel('category');
    document.getElementById('categoryModal').style.display = 'block';
}

function closeCategoryModal() {
    document.getElementById('categoryModal').style.display = 'none';
}

function openGroupModal() {
    if (!categories.length) {
        alert('Please wait for grid data to load!');
        return;
    }
    
    populateGroupCategories();
    syncAddPanel('group');
    const nameEl = document.getElementById('groupName');
    if (nameEl) { nameEl.value = ''; nameEl.placeholder = nextGroupName(); }
    const gs = document.getElementById('groupCategorySearch');
    if (gs) gs.value = '';
    document.getElementById('groupModal').style.display = 'block';
}

function closeGroupModal() {
    document.getElementById('groupModal').style.display = 'none';
}

function populateCategoryList() {
    const categoryList = document.getElementById('categoryList');
    categoryList.innerHTML = '';
    
    categories.forEach(category => {
        const item = document.createElement('div');
        item.className = 'category-item';
        item.dataset.category = category;
        
        // Create checkbox (hidden by CSS but functional)
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = `cat_${category}`;
        checkbox.value = category;
        checkbox.style.display = 'none'; // Hidden but functional
        
        // Create label
        const label = document.createElement('label');
        label.htmlFor = `cat_${category}`;
        label.textContent = category;
        label.style.cursor = 'pointer';
        label.style.width = '100%';
        
        // Add click handler to toggle selection visually and functionally
        item.addEventListener('click', function(e) {
            e.preventDefault();
            
            // Toggle checkbox state
            checkbox.checked = !checkbox.checked;
            
            // Toggle visual selection
            item.classList.toggle('selected', checkbox.checked);
        });
        
        item.appendChild(checkbox);
        item.appendChild(label);
        categoryList.appendChild(item);
    });
}

function getAlreadyAddedCategories() {
    const addedCategories = new Set();
    
    selectedItems.forEach(item => {
        if (item.type === 'feature') {
            // Single category feature
            addedCategories.add(item.name);
        } else if (item.type === 'group') {
            // Group - add all categories in the group
            item.data.forEach(categoryName => {
                addedCategories.add(categoryName);
            });
        }
    });
    
    return addedCategories;
}

function populateCategoryList() {
    const categoryList = document.getElementById('categoryList');
    categoryList.innerHTML = '';
    
    const alreadyAdded = getAlreadyAddedCategories();
    
    // Filter out already added categories
    const availableCategories = categories.filter(category => !alreadyAdded.has(category));
    
    if (availableCategories.length === 0) {
        const noItems = document.createElement('div');
        noItems.className = 'no-items';
        noItems.style.cssText = 'padding: 20px; text-align: center; color: #666; font-style: italic;';
        noItems.textContent = 'All categories have already been added';
        categoryList.appendChild(noItems);
        return;
    }
    
    availableCategories.forEach(category => {
        const item = document.createElement('div');
        item.className = 'category-item';
        item.dataset.category = category;
        
        // Create checkbox (hidden by CSS but functional)
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = `cat_${category}`;
        checkbox.value = category;
        checkbox.style.display = 'none'; // Hidden but functional
        
        // Create label
        const label = document.createElement('label');
        label.htmlFor = `cat_${category}`;
        label.textContent = category;
        label.style.cursor = 'pointer';
        label.style.width = '100%';
        
        // Add click handler to toggle selection visually and functionally
        item.addEventListener('click', function(e) {
            e.preventDefault();
            
            // Toggle checkbox state
            checkbox.checked = !checkbox.checked;
            
            // Toggle visual selection
            item.classList.toggle('selected', checkbox.checked);
        });
        
        item.appendChild(checkbox);
        item.appendChild(label);
        categoryList.appendChild(item);
    });
}

function populateGroupCategories() {
    const available = document.getElementById('availableCategories');
    const selected = document.getElementById('selectedCategories');
    
    available.innerHTML = '';
    selected.innerHTML = '';
    
    const alreadyAdded = getAlreadyAddedCategories();
    
    // Filter out already added categories
    const availableCategories = categories.filter(category => !alreadyAdded.has(category));
    
    if (availableCategories.length === 0) {
        const noItems = document.createElement('div');
        noItems.className = 'no-items';
        noItems.style.cssText = 'padding: 20px; text-align: center; color: #666; font-style: italic;';
        noItems.textContent = 'All categories have already been added';
        available.appendChild(noItems);
        return;
    }
    
    availableCategories.forEach(category => {
        const item = document.createElement('div');
        item.className = 'category-item';
        item.dataset.category = category;
        
        // Create hidden checkbox (functional but not visible)
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = category;
        checkbox.style.display = 'none';
        
        // Create span for text
        const span = document.createElement('span');
        span.textContent = category;
        
        // Add click handler for selection behavior
        item.addEventListener('click', function(e) {
            e.preventDefault();
            
            // Toggle checkbox state
            checkbox.checked = !checkbox.checked;
            
            // Toggle visual selection
            item.classList.toggle('selected', checkbox.checked);
        });
        
        item.appendChild(checkbox);
        item.appendChild(span);
        available.appendChild(item);
    });
}

function filterCategories() {
    const search = document.getElementById('categorySearch').value.toLowerCase();
    const items = document.querySelectorAll('#categoryList .category-item');
    const noItemsDiv = document.querySelector('#categoryList .no-items');
    
    let visibleCount = 0;
    
    items.forEach(item => {
        const categoryName = item.dataset.category.toLowerCase();
        const shouldShow = categoryName.includes(search);
        item.style.display = shouldShow ? 'flex' : 'none';
        if (shouldShow) visibleCount++;
    });
    
    // Handle the "no items" message for search results
    if (noItemsDiv) {
        noItemsDiv.style.display = 'none'; // Hide the original "no items" when searching
    }
    
    // Show "no results" message if searching but no matches
    const existingNoResults = document.querySelector('#categoryList .no-search-results');
    if (search && visibleCount === 0 && items.length > 0) {
        if (!existingNoResults) {
            const noResults = document.createElement('div');
            noResults.className = 'no-search-results';
            noResults.style.cssText = 'padding: 20px; text-align: center; color: #666; font-style: italic;';
            noResults.textContent = 'No categories match your search';
            document.getElementById('categoryList').appendChild(noResults);
        }
    } else if (existingNoResults) {
        existingNoResults.remove();
    }
}

// Updated selectAllCategories function to work with filtered categories
function selectAllCategories() {
    const items = document.querySelectorAll('#categoryList .category-item');
    const search = document.getElementById('categorySearch').value.toLowerCase();
    
    items.forEach(item => {
        // Only select visible items
        const isVisible = item.style.display !== 'none';
        const matchesSearch = !search || item.textContent.toLowerCase().includes(search);
        
        if (isVisible && matchesSearch) {
            const checkbox = item.querySelector('input[type="checkbox"]');
            checkbox.checked = true;
            item.classList.add('selected');
        }
    });
}

function moveToSelected() {
    const available = document.getElementById('availableCategories');
    const selected = document.getElementById('selectedCategories');
    const selectedItems = available.querySelectorAll('.category-item.selected');
    
    selectedItems.forEach(item => {
        // Reset selection state when moving
        item.classList.remove('selected');
        const checkbox = item.querySelector('input[type="checkbox"]');
        checkbox.checked = false;
        
        // Move to selected categories
        selected.appendChild(item);
    });
}

function moveToAvailable() {
    const available = document.getElementById('availableCategories');
    const selected = document.getElementById('selectedCategories');
    const selectedItems = selected.querySelectorAll('.category-item.selected');
    
    selectedItems.forEach(item => {
        // Reset selection state when moving
        item.classList.remove('selected');
        const checkbox = item.querySelector('input[type="checkbox"]');
        checkbox.checked = false;
        
        // Move back to available categories
        available.appendChild(item);
    });
}

function calculateWardScores(gridScores) {
    if (!gridData || !gridData.ward_assignments || !gridScores) {
        return {};
    }
    
    const wardScores = {};
    const wardCellCounts = {};
    
    // Group cells by ward and sum their scores
    Object.entries(gridScores).forEach(([cellId, score]) => {
        const wardName = gridData.ward_assignments[cellId];
        
        if (wardName) {
            if (!wardScores[wardName]) {
                wardScores[wardName] = 0;
                wardCellCounts[wardName] = 0;
            }
            wardScores[wardName] += score;
            wardCellCounts[wardName]++;
        }
    });
    
    // Calculate average score per ward
    Object.keys(wardScores).forEach(wardName => {
        if (wardCellCounts[wardName] > 0) {
            wardScores[wardName] = wardScores[wardName] / wardCellCounts[wardName];
        }
    });
    
    return wardScores;
}

function calculateWeightedScores() {
    if (!selectedItems.length || !gridData) return {};
    
    selectedItems.forEach(normaliseItem);
    const positiveItems = selectedItems.filter(item => !isNegativeItem(item));

    // Avoidance is purely subtractive: it removes score that wanted categories
    // put there, and never creates any. A selection with nothing wanted in it
    // therefore has nothing to subtract from and no meaningful score - showing
    // an empty area as "good" because it lacks the avoided thing would be
    // rewarding emptiness.
    if (positiveItems.length === 0) return {};

    // The universe is the cells reachable by something the user wants. Cells
    // whose k is zero for every selected category are omitted from the payload
    // and would score 0 anyway. Negative preferences do not widen it, since a
    // cell with nothing wanted nearby scores 0 whether or not it also has
    // something unwanted.
    const allCellIds = new Set();
    positiveItems.forEach(item => {
        item.data.forEach(category => {
            if (gridData.grid_scores[category]) {
                Object.keys(gridData.grid_scores[category]).forEach(cellId => {
                    allCellIds.add(cellId);
                });
            }
        });
    });

    if (allCellIds.size === 0) return {};

    const kFor = (item, cellId) => {
        const idx = item.distanceIndex ?? currentDistanceIndex;
        // NB: must go through getKValue - comparing the stored array directly
        // against 0 coerces to NaN and is always false.
        if (item.type === 'feature') return getKValue(item.data[0], cellId, idx);
        let total = 0;
        for (const category of item.data) total += getKValue(category, cellId, idx);
        return total;
    };

    const validCells = new Set(allCellIds);

    // Hard constraints first, so the soft terms never run on a doomed cell.
    for (const item of selectedItems) {
        const hard = CONSTRAINTS[constraintOf(item)].hard;
        if (!hard) continue;
        for (const cellId of validCells) {
            const k = kFor(item, cellId);
            if (hard === 'require' ? k === 0 : k > 0) validCells.delete(cellId);
        }
    }

    // Only what the user wants sets the scale.
    const totalWeight = positiveItems.reduce(
        (sum, item) => sum + weightOptions[item.weight], 0);
    if (totalWeight <= 0) return {};

    const finalScores = {};

    for (const cellId of validCells) {
        let weightedSum = 0;

        for (const item of selectedItems) {
            // 'never' is a hard filter only; surviving cells have k = 0 for it.
            if (CONSTRAINTS[constraintOf(item)].hard === 'exclude') continue;
            const k = kFor(item, cellId);
            if (k === 0) continue;          // saturating term is 0 either way

            // Positive adds; negative subtracts the same magnitude without
            // enlarging the denominator, so it can only ever lower the score.
            const term = weightOptions[item.weight] * (1 - Math.exp(-lambdaFor(item) * k));
            weightedSum += isNegativeItem(item) ? -term : term;
        }

        finalScores[cellId] = Math.max(0, weightedSum / totalWeight);
    }

    return finalScores;
}

function getColorFromScore(normalizedScore) {
    const colors = [
        [68, 1, 84],
        [49, 104, 142],
        [53, 183, 121],
        [173, 221, 142],
        [253, 231, 37]
    ];
    
    const scaledScore = normalizedScore * (colors.length - 1);
    const index = Math.floor(scaledScore);
    const fraction = scaledScore - index;
    
    if (index >= colors.length - 1) {
        const color = colors[colors.length - 1];
        return `rgb(${color[0]}, ${color[1]}, ${color[2]})`;
    }
    
    const color1 = colors[index];
    const color2 = colors[index + 1];
    
    const r = Math.round(color1[0] + (color2[0] - color1[0]) * fraction);
    const g = Math.round(color1[1] + (color2[1] - color1[1]) * fraction);
    const b = Math.round(color1[2] + (color2[2] - color1[2]) * fraction);
    
    return `rgb(${r}, ${g}, ${b})`;
}

function clearLayers() {
    closeCurrentTooltip();
    
    if (gridLayer) {
        map.removeLayer(gridLayer);
        gridLayer = null;
    }
    if (wardLayer) {
        map.removeLayer(wardLayer);
        wardLayer = null;
    }
}

function clearVisualization() {
    clearLayers();
    currentScores = {};
}

function showLoading() {
    document.getElementById('loading').style.display = 'block';
}

function hideLoading() {
    document.getElementById('loading').style.display = 'none';
}

// === UTILITY FUNCTIONS FOR DEBUGGING GRID ADJUSTMENTS ===

// Call this function in browser console to test different ratio values
function testGridRatios(ratios) {
    Object.assign(GRID_ADJUSTMENT_RATIOS, ratios);
    updateVisualization();
    console.log('Updated grid ratios:', GRID_ADJUSTMENT_RATIOS);
}

// Reset all ratios to zero
function resetGridRatios() {
    Object.keys(GRID_ADJUSTMENT_RATIOS).forEach(key => {
        if (typeof GRID_ADJUSTMENT_RATIOS[key] === 'number') {
            GRID_ADJUSTMENT_RATIOS[key] = 0;
        }
    });
    GRID_ADJUSTMENT_RATIOS.coordinatePrecision = 6;
    updateVisualization();
    console.log('Reset grid ratios to defaults');
}

// Quick fix for common gap issues - try this first
function quickGapFix() {
    GRID_ADJUSTMENT_RATIOS.overlapCompensation = 0.002;
    GRID_ADJUSTMENT_RATIOS.cellWidthRatio = -0.1;  // Reduce width by 10%
    GRID_ADJUSTMENT_RATIOS.cellHeightRatio = 0.1;  // Increase height by 10%
    GRID_ADJUSTMENT_RATIOS.coordinatePrecision = 6;
    updateVisualization();
    console.log('Applied quick gap fix with width -10%, height +10%');
}

// Test function specifically for your requirements
function testSizeAdjustments(widthChange, heightChange) {
    GRID_ADJUSTMENT_RATIOS.cellWidthRatio = widthChange;  // -0.1 = reduce by 10%
    GRID_ADJUSTMENT_RATIOS.cellHeightRatio = heightChange; // 0.1 = increase by 10%
    updateVisualization();
    console.log(`Applied width: ${widthChange * 100}%, height: ${heightChange * 100}%`);
}