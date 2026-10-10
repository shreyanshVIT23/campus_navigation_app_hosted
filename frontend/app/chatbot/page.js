"use client";
import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import { sendChatMessage, sendChatAudio, getApiUrl } from "@/lib/api";

// Formats the backend (Gemini) can transcribe, in order of preference
const RECORDING_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/ogg",
];

function pickRecordingMimeType() {
  if (typeof MediaRecorder === "undefined") return "";
  return RECORDING_MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function fileExtensionFor(mimeType) {
  if (mimeType.includes("ogg")) return "ogg";
  if (mimeType.includes("wav")) return "wav";
  return "webm";
}

const Chatbot = () => {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const inputRef = useRef(null);
  // Every floor map of the route, in walking order: [{ building, floor, url }]
  const [mapLegs, setMapLegs] = useState([]);
  const [legIndex, setLegIndex] = useState(0);
  const [isRecording, setIsRecording] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const mapRef = useRef(null);
  const recorderRef = useRef(null);
  const chunksRef = useRef([]);

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
    // Release the microphone if the page is left while recording
    return () => {
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.stream.getTracks().forEach((track) => track.stop());
      }
    };
  }, []);

  const addMessage = (text, sender) => {
    setMessages((prevMessages) => [...prevMessages, { text, sender }]);
  };

  const handleMapClose = () => {
    if (mapRef.current) {
      mapRef.current.style.display = "none";
    }
    setMapLegs([]);
    setLegIndex(0);
  };

  // The assistant returns map links when the answer is a route
  const showRoute = (files) => {
    if (!files) return;
    // Older responses only have the start and end maps
    const legs =
      files.legs && files.legs.length > 0
        ? files.legs
        : [files.start_floor, files.end_floor]
            .filter(Boolean)
            .map((url) => ({ building: null, floor: null, url }));
    if (legs.length === 0) return;
    setMapLegs(legs);
    setLegIndex(0);
    if (mapRef.current) {
      mapRef.current.style.display = "block";
    }
  };

  const handleSendMessage = async () => {
    if (!input.trim() || isSending) return;

    const currentInput = input;
    addMessage(currentInput, "user");
    setInput("");
    setIsSending(true);

    try {
      const data = await sendChatMessage(currentInput);
      addMessage(data.response || "Sorry, I didn't get a reply.", "server");
      showRoute(data.files);
    } catch (error) {
      console.error("Error sending message:", error);
      addMessage("Failed to get response from server.", "server");
    } finally {
      setIsSending(false);
    }
  };

  const sendRecording = async (blob) => {
    setIsSending(true);
    try {
      const data = await sendChatAudio(blob, `voice.${fileExtensionFor(blob.type)}`);
      addMessage(data.transcript ? `🎤 ${data.transcript}` : "🎤 (voice message)", "user");
      addMessage(data.response || "Sorry, I didn't get a reply.", "server");
      showRoute(data.files);
    } catch (error) {
      console.error("Error sending voice message:", error);
      addMessage(error.message || "Failed to send voice message.", "server");
    } finally {
      setIsSending(false);
    }
  };

  const toggleRecording = async () => {
    if (isRecording) {
      recorderRef.current?.stop();
      return;
    }
    if (isSending) return;

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      addMessage("Voice input isn't supported in this browser.", "server");
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = pickRecordingMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      chunksRef.current = [];

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        setIsRecording(false);
        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType || mimeType || "audio/webm",
        });
        if (blob.size > 0) sendRecording(blob);
      };

      recorderRef.current = recorder;
      recorder.start();
      setIsRecording(true);
    } catch (error) {
      console.error("Microphone error:", error);
      addMessage(
        error.name === "NotAllowedError"
          ? "Microphone permission was denied. Please allow it to ask by voice."
          : "Couldn't start recording.",
        "server"
      );
    }
  };

  const handleFocus = () => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
  };

  const router = useRouter();

  const handleClose = () => {
    router.push("/student"); // Redirect to "/student"
  };

  const currentLeg = mapLegs[legIndex];
  const legTitle = !currentLeg
    ? ""
    : currentLeg.building
      ? `${currentLeg.building} · floor ${currentLeg.floor}`
      : legIndex === 0
        ? "Start Floor Map"
        : "End Floor Map";

  return (
    <div className="flex flex-col h-screen w-full mx-auto bg-white shadow-lg rounded-lg border">
      {/* Header */}
      <div className="bg-[#007BFF] h-[100px] text-white p-4 flex justify-between items-center rounded-t-lg">
        <div className="flex items-center">
          <Image src="/logoSend.png" width={100} height={100} alt="logo" />
          <h2 className="font-bold text-[35px] text-[#000000]">Campus Assistant</h2>
        </div>
        <div className="flex min-w-[8%] justify-between items-center">
          <Image src="/profile.png" width={50} height={50} alt="logo" />
          {/* Close button */}
          <button onClick={handleClose}>
            <Image src="/Close_White.png" width={50} height={50} alt="close" className="cursor-pointer" />
          </button>
        </div>
      </div>

      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-2 min-w-[100%] ml-auto">
        {messages.map((msg, index) => (
          <div
            key={index}
            className={`p-3 rounded-lg w-fit max-w-[40%] ${msg.sender === "user"
              ? "bg-blue-500 text-white ml-auto text-right" // User messages align right
              : "bg-gray-300 text-black mr-auto text-left" // Server messages align left
              }`}
          >
            {msg.text.split("\n").map((line, i) => (
              <p key={i}>{line}</p>
            ))}
          </div>
        ))}
        {isSending && (
          <div className="p-3 rounded-lg w-fit bg-gray-200 text-gray-600 mr-auto text-left">
            Thinking...
          </div>
        )}
      </div>

      {/* Input & Buttons */}
      <div
        onClick={handleFocus}
        className="flex mx-auto mb-[5%] items-center p-2 bg-[rgba(217,217,217,0.5)] rounded-b-lg w-[80%] h-[100px] rounded-[10px] text-[#212529] cursor-text"
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            toggleRecording();
          }}
          disabled={isSending && !isRecording}
          title={isRecording ? "Stop recording and send" : "Ask by voice"}
          aria-pressed={isRecording}
          className={`rounded-full p-1 cursor-pointer disabled:opacity-50 ${isRecording ? "ring-4 ring-red-500 animate-pulse" : ""}`}
        >
          <Image src="/voice.png" width={50} height={50} alt={isRecording ? "stop recording" : "voice input"} />
        </button>

        <input
          ref={inputRef}
          type="text"
          className="flex-1 p-2 outline-none rounded-lg"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={isRecording ? "Listening... click the mic again to send" : "Ask something..."}
          onKeyDown={(e) => e.key === "Enter" && handleSendMessage()}
          autoFocus
        />

        <Image
          src="/send.png"
          width={50}
          height={50}
          alt="send"
          className="cursor-pointer"
          onClick={handleSendMessage}
        />
      </div>
      <div className="flex gap-6 mt-6 absolute top-5 left-150 z-50 text-black">
        {/* Map Container */}
        <div ref={mapRef} className="relative hidden bg-white p-4 shadow-md rounded-lg">
          {/* Right padding keeps the title clear of the buttons */}
          <h2 className="font-bold mb-2 pr-72">
            {legTitle}
            {mapLegs.length > 1 && ` (${legIndex + 1} of ${mapLegs.length})`}
          </h2>

          {/* Step through the floors only when the route spans more than one */}
          {mapLegs.length > 1 && (
            <div className="absolute top-2 right-16 flex gap-2 z-10">
              <button
                onClick={() => setLegIndex((i) => i - 1)}
                disabled={legIndex === 0}
                className="bg-gray-300 p-2 rounded-md shadow-md cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              >
                ◀ Previous
              </button>
              <button
                onClick={() => setLegIndex((i) => i + 1)}
                disabled={legIndex === mapLegs.length - 1}
                className="bg-gray-300 p-2 rounded-md shadow-md cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Next ▶
              </button>
            </div>
          )}
          <button
            onClick={handleMapClose}
            className="absolute top-2 right-2 bg-gray-300 p-2 rounded-md shadow-md z-10 cursor-pointer"
          >
            <Image src="/Close.png" width={30} height={30} alt="close" />
          </button>

          {currentLeg && (
            <iframe
              src={getApiUrl(currentLeg.url)}
              title="Route map"
              className="w-[500px] h-[500px] border rounded-lg"
            />
          )}
        </div>
      </div>
    </div>
  );
};

export default Chatbot;
