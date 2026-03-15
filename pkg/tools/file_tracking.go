package tools

import (
	"sync"
	"time"
)

type fileRecord struct {
	readTime  time.Time
	writeTime time.Time
}

var (
	fileRecords     = make(map[string]fileRecord)
	fileRecordMutex sync.RWMutex
)

func recordFileRead(path string) {
	fileRecordMutex.Lock()
	defer fileRecordMutex.Unlock()
	record := fileRecords[path]
	record.readTime = time.Now()
	fileRecords[path] = record
}

func getLastReadTime(path string) time.Time {
	fileRecordMutex.RLock()
	defer fileRecordMutex.RUnlock()
	record, ok := fileRecords[path]
	if !ok {
		return time.Time{}
	}
	return record.readTime
}

func recordFileWrite(path string) {
	fileRecordMutex.Lock()
	defer fileRecordMutex.Unlock()
	record := fileRecords[path]
	record.writeTime = time.Now()
	fileRecords[path] = record
}
