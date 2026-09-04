on run argv
	if (count of argv) is less than 2 then error "Usage: osascript export_keynote.scpt input.key output-dir [pptx]"
	set inputPath to item 1 of argv
	set outputDir to item 2 of argv
	set inputFile to POSIX file inputPath
	set baseName to my basename(inputPath)
	set outputFile to POSIX file (outputDir & "/" & baseName & ".pptx")
	tell application "Keynote"
		activate
		set theDoc to open inputFile
		export theDoc to outputFile as Microsoft PowerPoint
		close theDoc saving no
	end tell
end run

on basename(posixPath)
	set oldDelims to AppleScript's text item delimiters
	set AppleScript's text item delimiters to "/"
	set fileName to last text item of posixPath
	set AppleScript's text item delimiters to "."
	if (count of text items of fileName) > 1 then
		set stem to (text items 1 thru -2 of fileName) as text
	else
		set stem to fileName
	end if
	set AppleScript's text item delimiters to oldDelims
	return stem
end basename
