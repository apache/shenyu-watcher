/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.shenyu;

import org.apache.shenyu.entity.JarDO;
import org.apache.shenyu.env.CheckEnv;
import org.apache.shenyu.util.FileUtil;
import org.apache.shenyu.util.StringUtil;

import java.util.ArrayList;
import java.util.List;

public class ShenyuWatcher {

    public static void main(String[] args) {

        // check py version
        System.out.println("Start to check python version...");
        if (CheckEnv.PYTHON_CHECK) {
            System.out.println("The python version check passed.");

            String filePath = args[0];

            String fileName = filePath.substring(filePath.lastIndexOf("\\") + 1);

            System.out.println("Start to unzip " + fileName + "...");

            String destDir = filePath.substring(0, filePath.lastIndexOf("\\"));
            String fileDir = fileName.replace(".tar.gz", "");

            FileUtil.unTarGz(filePath, destDir);
            System.out.println("Decompression succeeded.");
            System.out.println("Start to check LICENSE...");
            List<String> fileNames = FileUtil.getFileName(destDir + "\\" + fileDir + "\\lib");

            JarDO jarDO = JarDO.build(fileNames);
            String content = FileUtil.read(destDir + "\\" + fileDir + "\\LICENSE");

            List<String> failureMatchJar = new ArrayList<>();

            for (JarDO.ParseJar parseJar : jarDO.getParseJar()) {

                if (parseJar.getOriginal().contains("shenyu")) {
                    continue;
                }

                if (!StringUtil.match(content, parseJar.getPackageName() + " " + parseJar.getVersion())) {
                    failureMatchJar.add(parseJar.getOriginal());
                }

            }

            if (failureMatchJar.size() > 0) {
                System.err.println("The following jars need to be modified: ");
                failureMatchJar.forEach(System.err::println);
            }

            jarDO.setFailureMatchJar(failureMatchJar);

        } else {
            System.err.println("The python version not 3.8");
        }

    }

}
