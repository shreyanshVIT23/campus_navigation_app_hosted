'use client';
import React, { useRef, useState } from 'react'
import style from '@/app/student/Student.module.css'
import Image from 'next/image';
import { processPath, multiBuildingProcessPath, customProcessPath, getApiUrl } from '@/lib/api';

const BUILDING_LABELS = {
    "AB-01": "Academic Block",
    "Lab-Complex": "Lab Complex",
};

// What the backend understands as "the nearest toilet of this kind"
const BATHROOM_REQUESTS = {
    male: "male toilet",
    female: "female toilet",
    any: "toilet",
};

// 400: what was typed can't be used; 422: the map has no way between the places.
// The backend's message already says what is wrong, so it is shown as it is.
const EXPLAINED_STATUSES = [400, 422];

async function routeErrorMessage(response) {
    const errData = await response.json().catch(() => ({}));
    const detailMsg = errData.detail || errData.message || errData.error || response.statusText;
    if (EXPLAINED_STATUSES.includes(response.status)) {
        return detailMsg;
    }
    return `Backend Server Error (${response.status}): ${detailMsg}\n\nSomething went wrong on the server. Please try again later.`;
}

function ShowPath({ buttonRef }) {

    // "" = no preference: the backend picks the shortest route by lift or stairs
    const [preference, setPreference] = useState("");
    const [bathroomFor, setBathroomFor] = useState("any");
    // e.g. "No lift connects floor 1 and floor 2, so this route uses the stairs."
    const [routeNote, setRouteNote] = useState("");

    const [startFloorMap, setStartFloorMap] = useState("");
    const [endFloorMap, setEndFloorMap] = useState("");
    const [startFloorMapBuilding2, setStartFloorMapBuilding2] = useState("")
    const [endFloorMapBuilding2, setEndFloorMapBuilding2] = useState("")
    const [showBuilding1Maps, setShowBuilding1Maps] = useState(true);
    const [loading, setLoading] = useState(false);
    const [showStartMap, setShowStartMap] = useState(true);
    const [start, setStart] = useState("");
    const [end, setEnd] = useState("");
    const [svgData, setSvgData] = useState(null);
    const mapRef = useRef(null);

    const [navType, setNavType] = useState(""); // "single", "multi", "bathroom" or "custom"
    const [building, setBuilding] = useState("AB-01"); // for same-building routes
    const [building1, setBuilding1] = useState("");
    const [building2, setBuilding2] = useState("");


    // Clear maps from a previous search (the inline SVG would otherwise stay on screen)
    const resetMaps = () => {
        setStartFloorMap('')
        setEndFloorMap('')
        setStartFloorMapBuilding2('')
        setEndFloorMapBuilding2('')
        setSvgData(null)
        setShowBuilding1Maps(true)
        setRouteNote('')
    }

    // Function to handle API request
    const handleSearch = async () => {
        const destination = navType === 'bathroom' ? BATHROOM_REQUESTS[bathroomFor] : end;
        if (!start || !destination) {
            alert(navType === 'bathroom'
                ? "Please enter where you are now."
                : "Please enter both start and end locations.");
            return;
        }
        resetMaps()

        setLoading(true);

        try {
            const request = { start, end: destination, preference: preference || null, building };
            const response = navType === 'custom'
              ? await customProcessPath({ type: "custom", ...request })
              : await processPath(request);

            if (response.ok) {
                const contentType = response.headers.get("Content-Type") || "";

                if (contentType.includes("application/json")) {
                    // Complex Path - JSON Response
                    const data = await response.json();
                    if (data.error) {
                        alert(data.error);
                        return;
                    }

                    setStartFloorMap(data.files?.start_floor || "");
                    setEndFloorMap(data.files?.end_floor || "");
                    setSvgData(null); // Reset SVG data
                    setRouteNote(data.note || "");
                } else if (contentType.includes("image/svg+xml")) {
                    // Same Floor - SVG Response
                    const svgText = await response.text();
                    setSvgData(svgText); // Store the SVG text
                    setStartFloorMap(null);
                    setEndFloorMap(null);
                } else {
                    console.error("Unexpected response format");
                }
            } else {
                alert(await routeErrorMessage(response));
                return;
            }
        } catch (error) {
            alert("Request failed: " + error.message);
            return;
        } finally {
            setLoading(false);
        }
        if (mapRef.current) {
            mapRef.current.style.display = 'block';
            handleSearchClose();
        }
    };

    const handleSearchMulti = async () => {
        if (!start || !end) {
            alert("Please enter both start and end locations.");
            return;
        }
        if (!building1 || !building2) {
            alert("Please choose the start building and the destination building.");
            return;
        }
        if (building1 === building2) {
            alert("Both locations are in the same building. Use 'Same Building' instead.");
            return;
        }
        resetMaps()
        setLoading(true);
        try {
            const response = await multiBuildingProcessPath({
                "Start Location": start,
                "End Location": end,
                "building_name_1": building1,
                "building_name_2": building2
            });

            if (response.ok) {
                const data = await response.json();

                // Building 1: from your room to its exit; building 2: entrance to your room
                const files1 = data.files?.[building1] || {};
                const files2 = data.files?.[building2] || {};
                setStartFloorMap(files1.start_floor || "");
                setEndFloorMap(files1.end_floor || "");
                setStartFloorMapBuilding2(files2.start_floor || "");
                setEndFloorMapBuilding2(files2.end_floor || "");
            } else {
                alert(await routeErrorMessage(response));
                return;
            }
        } catch (error) {
            alert("Request failed: " + error.message);
            return;
        } finally {
            setLoading(false);
        }
        if (mapRef.current) {
            mapRef.current.style.display = 'block';
            handleSearchClose();
        }
    }



    const swapBuilding = () => {
        if (navType !== 'multi') {
            return
        }
        setShowBuilding1Maps(prev => !prev);
    };



    const handleSearchClose = () => {
        if (buttonRef.current) {
            buttonRef.current.style.display = "none";
        }
    }


    const handleMapClose = () => {
        if (mapRef.current) {
            mapRef.current.style.display = 'none';
        }
        resetMaps()
    }

    const currentStartMap = showBuilding1Maps ? startFloorMap : startFloorMapBuilding2;
    const currentEndMap = showBuilding1Maps ? endFloorMap : endFloorMapBuilding2;
    const shownBuilding = navType === 'multi'
        ? (showBuilding1Maps ? building1 : building2)
        : building;

    return (
        <div>
            <div ref={buttonRef} className="absolute hidden top-0 left-[7%] bg-white text-black shadow-[0px_4px_4px_0px_#00000040] rounded-lg p-4 w-96 z-50">
                {/* Close Button */}
                <div className="flex justify-end mb-2">
                    <button className="text-gray-500 hover:text-black cursor-pointer text-2xl" onClick={handleSearchClose}>&times;</button>
                </div>

                {/* Input Section */}
                <select
                    className="border p-2 rounded-md w-full text-black mb-2"
                    value={navType}
                    onChange={(e) => setNavType(e.target.value)}
                >
                    <option value="" disabled>Select navigation type</option>
                    <option value="single">Same Building</option>
                    <option value="multi">Multi-Building</option>
                    <option value="bathroom">Nearest Bathroom</option>
                </select>

                {/* Input with Start Icon */}
                <div className="flex items-center border p-2 rounded-md mb-2 bg-white">
                    <Image src="/start.png" alt="Start" width={20} height={20} className="mr-2" />
                    <input
                        type="text"
                        placeholder={navType === 'bathroom' ? "Where are you now? (room, cabin or teacher)" : "Choose starting location"}
                        className="w-full outline-none text-black"
                        value={start}
                        onChange={(e) => setStart(e.target.value)}
                    />
                </div>

                {/* Input with End Icon (the bathroom finder picks the destination itself) */}
                {navType !== 'bathroom' && (
                    <div className="flex items-center border p-2 rounded-md mb-2 bg-white">
                        <Image src="/destination.png" alt="End" width={20} height={20} className="mr-2" />
                        <input
                            type="text"
                            placeholder="Choose end location"
                            className="w-full outline-none text-black"
                            value={end}
                            onChange={(e) => setEnd(e.target.value)}
                        />
                    </div>
                )}

                {/* Which bathroom: boys (B and some W rooms), girls (G and some W rooms) or whichever is nearest */}
                {navType === 'bathroom' && (
                    <select
                        className="border p-2 rounded-md w-full text-black mb-2"
                        value={bathroomFor}
                        onChange={(e) => setBathroomFor(e.target.value)}
                        aria-label="Bathroom type"
                    >
                        <option value="any">Nearest bathroom (any)</option>
                        <option value="male">Male bathroom</option>
                        <option value="female">Female bathroom</option>
                    </select>
                )}

                {/* Conditional Building Inputs for Multi-Building */}
                {navType === "multi" && (
                    <>
                        <select
                            className="border p-2 rounded-md w-full text-black mb-2"
                            value={building1}
                            onChange={(e) => setBuilding1(e.target.value)}
                        >
                            <option value="" disabled>Start building</option>
                            <option value="AB-01">AB-01</option>
                            <option value="Lab-Complex">Lab-Complex</option>
                        </select>
                        <select
                            className="border p-2 rounded-md w-full text-black mb-2"
                            value={building2}
                            onChange={(e) => setBuilding2(e.target.value)}
                        >
                            <option value="" disabled>Destination building</option>
                            <option value="AB-01">AB-01</option>
                            <option value="Lab-Complex">Lab-Complex</option>
                        </select>
                    </>
                )}

                {/* Building selector for same-building routes */}
                {(navType === "single" || navType === "bathroom") && (
                    <select
                        className="border p-2 rounded-md w-full text-black mb-2"
                        value={building}
                        onChange={(e) => setBuilding(e.target.value)}
                    >
                        <option value="AB-01">Academic Block (AB-01)</option>
                        <option value="Lab-Complex">Lab Complex</option>
                    </select>
                )}

                {/* Preference Selector ("" sends no preference: fastest of lift or stairs) */}
                {(navType === 'single' || navType === 'bathroom') && (
                    <select
                        className="border mt-2 p-2 rounded-md w-full text-black"
                        value={preference}
                        onChange={(e) => setPreference(e.target.value)}
                        aria-label="Lift or stairs preference"
                    >
                        <option value="">Fastest route (lift or stairs)</option>
                        <option value="Stairs">Prefer stairs</option>
                        <option value="Lift">Prefer lift</option>
                    </select>
                )}

                {/* Search Button */}
                <button
                    onClick={navType === 'multi' ? handleSearchMulti : handleSearch}
                    className="bg-blue-500 text-white p-3 rounded-md w-full mt-4 cursor-pointer disabled:opacity-50"
                    disabled={loading || !navType}
                >
                    {loading ? "Loading..." : "Search"}
                </button>
            </div>


            <div className="flex gap-6 absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 z-50">
                {/* Map Container */}
                <div ref={mapRef} className="relative hidden bg-white rounded-lg border-3">
                    <div className='flex justify-between items-center w-[100%] p-4 h-[50px]' style={{ borderBottom: '3px solid #000' }}>
                        <button
                            onClick={swapBuilding}
                            className=" p-2 rounded-md z-10 cursor-pointer"
                        >
                            <Image src='/prev.png' width={25} height={25} alt='previous' style={{ transform: 'rotate(180deg)' }} />
                        </button>
                        <h2 className="font-bold text-xl mb-2">
                            {BUILDING_LABELS[shownBuilding] || 'Select Navigation Type'}
                        </h2>

                        {/* Swap Button Inside iFrame */}
                        <div className='flex items-center'>
                            <button
                                className=" p-2 rounded-md z-10 cursor-pointer"
                                onClick={swapBuilding}
                            >
                                <Image src='/next.png' width={25} height={25} alt='next' />
                            </button>
                            <button
                                onClick={handleMapClose}
                                className="p-2 rounded-md z-10 cursor-pointer"
                            >
                                <Image src='/Close.png' width={30} height={30} alt='close' />
                            </button>
                        </div>
                    </div>

                    {routeNote && (
                        <p role="status" className="bg-yellow-100 text-black text-sm px-4 py-2" style={{ borderBottom: '3px solid #000' }}>
                            {routeNote}
                        </p>
                    )}

                    <div className='flex'>
                        {/* Start floor */}
                        <div style={{ borderRight: '3px solid #000' }}>
                            <h1 style={{ borderBottom: '3px solid #000' }} className='text-center font-bold text-xl'>Start Location</h1>

                            {svgData ? (
                                // Display SVG directly for same floor
                                <div dangerouslySetInnerHTML={{ __html: svgData }} className="w-[400px] h-[400px]" />
                            ) : (
                                // For different floor
                                currentStartMap && (
                                    <iframe
                                        src={getApiUrl(currentStartMap)}
                                        title="Start floor map"
                                        className="w-[400px] h-[400px]"
                                    />
                                )

                            )}
                        </div>

                        {/* End floor */}

                        <div>
                            {!svgData && currentEndMap && (
                                <>
                                    <h1 style={{ borderBottom: '3px solid #000' }} className='text-center font-bold text-xl'>End Location</h1>
                                    <iframe
                                        src={getApiUrl(currentEndMap)}
                                        title="End floor map"
                                        className="w-[400px] h-[400px]"
                                    />
                                </>
                            )}



                        </div>
                    </div>

                </div>
            </div>
        </div>
    )
}

export default ShowPath