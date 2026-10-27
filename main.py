from windows_capture import WindowsCapture, Frame, InternalCaptureControl

# Any error from on_closed and on_frame_arrived will surface here
capture = WindowsCapture(
    cursor_capture=None,
    draw_border=None,
    monitor_index=None,
    window_name="Brave",
)


# Called every time a new frame is available
@capture.event
def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
    print("New frame arrived")

    # Save the frame as an image to the specified path
    frame.save_as_image("image.png")

    # Gracefully stop the capture thread
    capture_control.stop()


# Called when the capture item closes (usually when the window closes).
# The capture session will end after this function returns.
@capture.event
def on_closed():
    print("Capture session closed")


capture.start()